//! ppstream: native UDP stream sender / receiver for the IEC 61162-450 payload test.
//!
//! WHY: the bulk streams (ECDIS 60, ONF database 500, other ONF 25 Mbit/s = up to ~43,000 packets per second each) are too heavy for the
//! Python endpoint on a small CPU: Python spends 10-20 us per packet in system calls. This program moves the same packets with
//! `recvmmsg` / `sendmmsg` (up to 64 packets per system call) and needs a few percent of one core. The 450 sentences, the radar, the
//! scenario handshake and the summary stay in Python (traffic/pp.py); Python starts one `ppstream` per stream and direction and reads
//! the small json file each one writes.
//!
//! PACKET FORMAT (identical to the Python endpoints; 1400-byte UDP payload):
//!     bytes 0..3   magic "FLL"
//!     byte  3      flow id (0..flows-1)
//!     bytes 4..12  sequence number, big endian u64, counted per flow
//!     bytes 12..16 offset into the chart pool, big endian u32
//!     bytes 16..   chart bytes (real S-57 chart data taken from the pool file)
//! A receiver counts a gap in the per-flow sequence numbers as loss (duplicates and reordering are not loss).
//!
//! USAGE
//!     ppstream recv --ip 192.168.50.2 --port 20100 --run-for 620 --stats rx.json [--stop-file F] [--rcvbuf BYTES]
//!     ppstream send --my-ip 192.168.50.1 --peer-ip 192.168.50.2 --port 20100 --mbps 500 --cos 1 --pool pool.bin
//!                   --start-in 8 --duration 600 --stats tx.json [--flows N] [--vary-min 0.25] [--vary-step 2] [--stop-file F]
//!   Stats files are rewritten atomically 5 times a second and once more at the end:
//!     recv: {"rx_pkts":N,"rx_bytes":N,"rx_lost":N,"final":bool}      send: {"tx_pkts":N,"final":bool}
//!   --mbps is the MAXIMUM wire rate (payload + 46 bytes of Ethernet/IP/UDP headers per packet); with --vary-min the rate wobbles between
//!   that fraction and the maximum (a new level every --vary-step seconds, now and then a peak at the maximum or a dip to the minimum).
//! Lab tool: no multicast, unicast UDP only, runs without root.

use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::Write;
use std::net::{Ipv4Addr, SocketAddrV4, UdpSocket};
use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::process::exit;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// UDP payload bytes of one stream packet.
const PAYLOAD: usize = 1400;
/// Header bytes at the start of every stream packet (magic, flow id, sequence, pool offset).
const HDR: usize = 16;
/// Ethernet + FCS (18) + IP (20) + UDP (8) bytes added on the wire to every payload.
const WIRE_OVERHEAD: usize = 46;
/// Packets handled per system call.
const BATCH: usize = 64;
/// Sequence numbers are compared modulo 2^62 (same as the Python endpoint).
const SEQ_MOD: u64 = 1 << 62;

/// Command line as a map `--name value`; flags without value get "1".
fn parse_args(args: &[String]) -> HashMap<String, String> {
    let mut m = HashMap::new();
    let mut i = 0;
    while i < args.len() {
        if let Some(k) = args[i].strip_prefix("--") {
            if i + 1 < args.len() && !args[i + 1].starts_with("--") {
                m.insert(k.to_string(), args[i + 1].clone());
                i += 2;
            } else {
                m.insert(k.to_string(), "1".to_string());
                i += 1;
            }
        } else {
            i += 1;
        }
    }
    m
}

/// Required argument, parsed; exits with a message if missing or malformed.
fn need<T: std::str::FromStr>(a: &HashMap<String, String>, k: &str) -> T {
    match a.get(k).and_then(|v| v.parse().ok()) {
        Some(v) => v,
        None => {
            eprintln!("ppstream: missing or bad --{}", k);
            exit(2)
        }
    }
}

/// Optional argument with default.
fn opt<T: std::str::FromStr>(a: &HashMap<String, String>, k: &str, d: T) -> T {
    a.get(k).and_then(|v| v.parse().ok()).unwrap_or(d)
}

/// Writes `body` to `path` atomically (temporary file, then rename) so a reader never sees half a file.
fn write_atomic(path: &str, body: &str) {
    let tmp = format!("{}.tmp", path);
    if let Ok(mut f) = fs::File::create(&tmp) {
        let _ = f.write_all(body.as_bytes());
        let _ = fs::rename(&tmp, path);
    }
}

/// Sets an integer socket option (errors are ignored: a kernel may cap the value, which is fine).
fn setopt(fd: i32, level: i32, name: i32, val: i32) {
    unsafe {
        libc::setsockopt(fd, level, name, &val as *const i32 as *const libc::c_void, std::mem::size_of::<i32>() as u32);
    }
}

/// Small xorshift random generator (no dependency); used only for the rate variation.
struct Rng(u64);
impl Rng {
    fn new() -> Self {
        let t = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(1);
        Rng(t | 1)
    }
    /// Uniform float in [0, 1).
    fn next(&mut self) -> f64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// Rate factor in [lo, 1]: redrawn every `step` seconds (usually uniform in [lo, 1], 15 % a peak at 1.0, 10 % a dip to lo).
struct Vary {
    lo: f64,
    step: f64,
    f: f64,
    next_at: f64,
    rng: Rng,
}
impl Vary {
    fn new(lo: f64, step: f64) -> Self {
        Vary { lo, step, f: 1.0, next_at: 0.0, rng: Rng::new() }
    }
    /// Factor at `t` seconds since the start.
    fn factor(&mut self, t: f64) -> f64 {
        if self.lo >= 1.0 {
            return 1.0;
        }
        if t >= self.next_at {
            self.next_at = t + self.step;
            let r = self.rng.next();
            self.f = if r < 0.15 { 1.0 } else if r < 0.25 { self.lo } else { self.lo + (1.0 - self.lo) * self.rng.next() };
        }
        self.f
    }
}

/// Receiver: counts packets, bytes and per-flow sequence gaps until `--run-for` seconds passed or the stop file exists.
fn recv(a: &HashMap<String, String>) {
    let ip: Ipv4Addr = need(a, "ip");
    let port: u16 = need(a, "port");
    let run_for: f64 = need(a, "run-for");
    let stats: String = need(a, "stats");
    let stop_file: Option<String> = a.get("stop-file").cloned();
    let sock = UdpSocket::bind(SocketAddrV4::new(ip, port)).unwrap_or_else(|e| {
        eprintln!("ppstream: bind {}:{}: {}", ip, port, e);
        exit(1)
    });
    let fd = sock.as_raw_fd();
    setopt(fd, libc::SOL_SOCKET, libc::SO_RCVBUF, opt(a, "rcvbuf", 32 << 20)); // the kernel caps this at net.core.rmem_max
    let tv = libc::timeval { tv_sec: 0, tv_usec: 100_000 };
    unsafe {
        libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_RCVTIMEO, &tv as *const _ as *const libc::c_void, std::mem::size_of::<libc::timeval>() as u32);
    }

    // BATCH buffers of 2048 bytes, one iovec and one mmsghdr each; the buffers never move.
    let mut bufs = vec![0u8; BATCH * 2048];
    let mut iovs: Vec<libc::iovec> = (0..BATCH).map(|i| libc::iovec { iov_base: bufs[i * 2048..].as_mut_ptr() as *mut libc::c_void, iov_len: 2048 }).collect();
    let mut msgs: Vec<libc::mmsghdr> = (0..BATCH)
        .map(|i| {
            let mut m: libc::mmsghdr = unsafe { std::mem::zeroed() };
            m.msg_hdr.msg_iov = &mut iovs[i] as *mut libc::iovec;
            m.msg_hdr.msg_iovlen = 1;
            m
        })
        .collect();

    let mut last: [Option<u64>; 256] = [None; 256]; // last sequence number seen per flow id
    let (mut pkts, mut bytes, mut lost) = (0u64, 0u64, 0u64);
    let t0 = Instant::now();
    let mut last_pub = Instant::now() - Duration::from_secs(1);
    loop {
        let n = unsafe { libc::recvmmsg(fd, msgs.as_mut_ptr(), BATCH as u32, libc::MSG_WAITFORONE, std::ptr::null_mut()) };
        for i in 0..(if n > 0 { n as usize } else { 0 }) {
            let len = msgs[i].msg_len as usize;
            let p = &bufs[i * 2048..i * 2048 + len.min(2048)];
            if len >= HDR && &p[0..3] == b"FLL" {
                pkts += 1;
                bytes += len as u64;
                let flow = p[3] as usize;
                let seq = u64::from_be_bytes([p[4], p[5], p[6], p[7], p[8], p[9], p[10], p[11]]);
                match last[flow] {
                    None => last[flow] = Some(seq),
                    Some(l) => {
                        let d = seq.wrapping_sub(l) % SEQ_MOD;
                        if d != 0 && d <= SEQ_MOD / 2 {
                            lost += d - 1; // a jump of d means d-1 packets are missing; d == 0 or "negative" = duplicate / reordered
                            last[flow] = Some(seq);
                        }
                    }
                }
            }
        }
        let done = t0.elapsed().as_secs_f64() >= run_for || stop_file.as_ref().map_or(false, |f| Path::new(f).exists());
        if done || last_pub.elapsed() >= Duration::from_millis(200) {
            last_pub = Instant::now();
            write_atomic(&stats, &format!("{{\"rx_pkts\":{},\"rx_bytes\":{},\"rx_lost\":{},\"final\":{}}}", pkts, bytes, lost, done));
        }
        if done {
            break;
        }
    }
}

/// Sender: paced packets at up to `--mbps` (wire), in batches of at most BATCH, over `--flows` source ports.
fn send(a: &HashMap<String, String>) {
    let my_ip: Ipv4Addr = need(a, "my-ip");
    let peer_ip: Ipv4Addr = need(a, "peer-ip");
    let port: u16 = need(a, "port");
    let mbps: f64 = need(a, "mbps");
    let cos: i32 = opt(a, "cos", 1);
    let pool_path: String = need(a, "pool");
    let start_in: f64 = opt(a, "start-in", 0.0);
    let duration: f64 = need(a, "duration");
    let stats: String = need(a, "stats");
    let flows: usize = opt::<usize>(a, "flows", 1).clamp(1, 255);
    let vary_min: f64 = opt(a, "vary-min", 1.0);
    let vary_step: f64 = opt(a, "vary-step", 2.0);
    let stop_file: Option<String> = a.get("stop-file").cloned();

    let pool = fs::read(&pool_path).unwrap_or_else(|e| {
        eprintln!("ppstream: pool {}: {}", pool_path, e);
        exit(1)
    });
    let body = PAYLOAD - HDR;
    if pool.len() < body * 2 {
        eprintln!("ppstream: pool too small");
        exit(1);
    }
    let socks: Vec<UdpSocket> = (0..flows)
        .map(|_| {
            let s = UdpSocket::bind(SocketAddrV4::new(my_ip, 0)).unwrap_or_else(|e| {
                eprintln!("ppstream: bind {}: {}", my_ip, e);
                exit(1)
            });
            setopt(s.as_raw_fd(), libc::IPPROTO_IP, libc::IP_TOS, (cos * 8) << 2); // DSCP = class * 8
            setopt(s.as_raw_fd(), libc::SOL_SOCKET, libc::SO_SNDBUF, 4 << 20);
            s.connect(SocketAddrV4::new(peer_ip, port)).unwrap_or_else(|e| {
                eprintln!("ppstream: connect: {}", e);
                exit(1)
            });
            s
        })
        .collect();

    let max_pps = mbps * 1e6 / 8.0 / (PAYLOAD + WIRE_OVERHEAD) as f64;
    let mut vary = Vary::new(vary_min, vary_step);
    let mut pk = vec![0u8; BATCH * PAYLOAD]; // one batch of packets
    let mut seq_per_flow = vec![0u64; flows];
    let (mut sent, mut off) = (0u64, 0usize);
    let mut credit = 0.0f64;

    // wait for the start time
    let t_launch = Instant::now();
    while t_launch.elapsed().as_secs_f64() < start_in {
        if stop_file.as_ref().map_or(false, |f| Path::new(f).exists()) {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let t0 = Instant::now();
    let mut last = t0;
    let mut last_pub = t0 - Duration::from_secs(1);
    loop {
        let now = Instant::now();
        let t = now.duration_since(t0).as_secs_f64();
        let done = t >= duration || stop_file.as_ref().map_or(false, |f| Path::new(f).exists());
        if done || now.duration_since(last_pub) >= Duration::from_millis(200) {
            last_pub = now;
            write_atomic(&stats, &format!("{{\"tx_pkts\":{},\"final\":{}}}", sent, done));
        }
        if done {
            break;
        }
        let pps = max_pps * vary.factor(t);
        credit += now.duration_since(last).as_secs_f64() * pps;
        last = now;
        if credit > pps * 0.05 + BATCH as f64 {
            credit = BATCH as f64; // cannot keep up: skip ahead instead of bursting
        }
        let n = (credit as usize).min(BATCH);
        if n == 0 {
            std::thread::sleep(Duration::from_micros(100));
            continue;
        }
        // build n packets, then send each flow's packets with one sendmmsg
        let mut per_flow: Vec<Vec<usize>> = vec![Vec::new(); flows];
        for k in 0..n {
            let fl = ((sent + k as u64) % flows as u64) as usize;
            let p = &mut pk[k * PAYLOAD..(k + 1) * PAYLOAD];
            p[0..3].copy_from_slice(b"FLL");
            p[3] = fl as u8;
            p[4..12].copy_from_slice(&seq_per_flow[fl].to_be_bytes());
            seq_per_flow[fl] += 1;
            p[12..16].copy_from_slice(&(off as u32).to_be_bytes());
            p[HDR..].copy_from_slice(&pool[off..off + body]);
            off = (off + body) % (pool.len() - body);
            per_flow[fl].push(k);
        }
        for fl in 0..flows {
            if per_flow[fl].is_empty() {
                continue;
            }
            let mut iovs: Vec<libc::iovec> = per_flow[fl].iter().map(|&k| libc::iovec { iov_base: pk[k * PAYLOAD..].as_ptr() as *mut libc::c_void, iov_len: PAYLOAD }).collect();
            let mut msgs: Vec<libc::mmsghdr> = (0..iovs.len())
                .map(|i| {
                    let mut m: libc::mmsghdr = unsafe { std::mem::zeroed() };
                    m.msg_hdr.msg_iov = &mut iovs[i] as *mut libc::iovec;
                    m.msg_hdr.msg_iovlen = 1;
                    m
                })
                .collect();
            let mut done_n = 0usize;
            while done_n < msgs.len() {
                let r = unsafe { libc::sendmmsg(socks[fl].as_raw_fd(), msgs[done_n..].as_mut_ptr(), (msgs.len() - done_n) as u32, 0) };
                if r > 0 {
                    done_n += r as usize;
                } else {
                    std::thread::sleep(Duration::from_micros(50)); // send buffer full: wait a moment
                }
            }
        }
        sent += n as u64;
        credit -= n as f64;
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: ppstream recv|send --help-in-source (see the top of src/main.rs)");
        exit(2);
    }
    let a = parse_args(&args[2..]);
    match args[1].as_str() {
        "recv" => recv(&a),
        "send" => send(&a),
        _ => {
            eprintln!("ppstream: first argument must be recv or send");
            exit(2)
        }
    }
}
