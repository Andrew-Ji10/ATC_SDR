/*
 * iqstream: stream the Pluto's received I/Q samples over TCP, packed to 12 bits.
 *
 * Runs on the radio, from RAM (/tmp/iqstream: a reboot removes it), in place of
 * iiod for the sample stream only; tuning, gain and sample rate stay with iiod.
 * Built and started by the Pi: pi/build_iqstream.sh, atcsdr/iqstream.py.
 *
 * Why: the AD9361's samples are 12 bits, but iiod sends them in 16-bit slots,
 * and the radio's ARM core can't push 86 MB/s (21.6 MSPS) through TCP; packed,
 * that's 65 MB/s. And iiod lands on either core at random, sometimes the one
 * that handles the Ethernet interrupts: these threads are pinned instead.
 *
 * Capture (-m): "mmap" (default) maps the kernel's DMA blocks, as iiod does, so
 * nothing is copied: a block goes from the reader to a sender, which packs it
 * (or sends it as it is with -f 16) and hands it back to the DMA. "read" reads
 * the IIO device into a ring of chunks instead (a copy per sample; the
 * fallback when the block interface isn't there). 1-2 sender threads, one TCP
 * connection each, take the oldest chunk; chunks are numbered, so the receiver
 * puts them back in order.
 *
 * Every chunk on the wire: a 28-byte little-endian header, then the samples:
 *   uint32 magic "IQS1", seq (0, 1, 2 ...), samples, flags, lost (chunks dropped
 *   just before this one); uint64 t_ns (CLOCK_REALTIME when it was read)
 *   flags: 1 = packed 12-bit, 3 bytes a sample: b0 = I[7:0],
 *          b1 = I[11:8] | Q[3:0] << 4, b2 = Q[11:4]; else int16 I, int16 Q
 *          2 = test pattern (-t), not the radio
 *
 *   iqstream [-p port] [-n connections] [-b samples per chunk] [-s chunks]
 *            [-f 12|16] [-m mmap|read] [-k] [-c reader,sender0,sender1 cores]
 *            [-t] [-r test MSPS] [-d IIO device name]
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define VERSION "2"
#define MAGIC 0x31535149u            /* "IQS1" */
#define FLAG_PACKED12 1u
#define FLAG_TEST 2u
#define MAX_CONNS 2
#define MAX_SLOTS 32

/* The IIO DMA block interface (ADI kernels; what libiio's local backend uses). */
struct block_alloc_req {
    uint32_t type, size, count, id;
};
struct block {
    uint32_t id, size, bytes_used, type, flags, offset;
    uint64_t timestamp;
};
#define BLOCK_ALLOC_IOCTL _IOWR('i', 0xa0, struct block_alloc_req)
#define BLOCK_FREE_IOCTL _IO('i', 0xa1)
#define BLOCK_QUERY_IOCTL _IOWR('i', 0xa2, struct block)
#define BLOCK_ENQUEUE_IOCTL _IOWR('i', 0xa3, struct block)
#define BLOCK_DEQUEUE_IOCTL _IOWR('i', 0xa4, struct block)

struct header {
    uint32_t magic, seq, samples, flags, lost;
    uint64_t t_ns;
} __attribute__((packed));

enum { FREE, FILLED, SENDING };
struct slot {
    int state;
    uint32_t seq, lost, samples;
    uint64_t t_ns;
    uint32_t *raw;                    /* I | Q << 16 per sample: ring memory or a DMA block */
    struct block blk;                 /* mmap mode: the DMA block this slot is */
};

static struct {
    int port, conns, slots, packed, test, mmap_mode, copy_first;
    size_t samples;
    double test_msps;
    int cpu_reader, cpu_sender[MAX_CONNS];
    const char *device;
} opt = {5300, 2, 8, 1, 0, 1, 0, 1 << 17, 0.0, 0, {1, 0}, "cf-ad9361-lpc"};
/* Default cores: the Ethernet interrupts land on core 0, so the first sender
 * (the only one with -n 1) runs on core 1; the reader shares core 0. */

static struct slot ring[MAX_SLOTS];
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t filled = PTHREAD_COND_INITIALIZER;
static volatile sig_atomic_t running = 1;
static uint64_t sent_chunks, dropped_chunks, overrange;
static char iio_dir[300];
static int iio_fd = -1;
static pid_t tids[1 + MAX_CONNS];     /* reader, sender0, sender1: for the CPU report */

static void die(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "iqstream: ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
    exit(1);
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + ts.tv_nsec;
}

static void pin(int cpu)
{
    cpu_set_t set;
    if (cpu < 0)
        return;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (pthread_setaffinity_np(pthread_self(), sizeof set, &set))
        fprintf(stderr, "iqstream: couldn't pin a thread to core %d\n", cpu);
}

/* CPU time (ticks) a thread of this process has used, from /proc. */
static long thread_ticks(pid_t tid)
{
    char path[64], buf[512];
    snprintf(path, sizeof path, "/proc/self/task/%d/stat", (int)tid);
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    char *p = strrchr(buf, ')');
    long utime = 0, stime = 0;
    /* After the command name: state, then 10 fields, then utime, stime. */
    if (!p || sscanf(p + 2, "%*c %*d %*d %*d %*d %*d %*u %*u %*u %*u %*u %ld %ld", &utime, &stime) != 2)
        return -1;
    return utime + stime;
}

/* ------------------------------------------------------------ IIO (sysfs) */

static int read_attr(const char *path, char *buf, size_t n)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    if (!fgets(buf, (int)n, f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    buf[strcspn(buf, "\n")] = 0;
    return 0;
}

static int write_attr(const char *path, const char *val)
{
    FILE *f = fopen(path, "w");
    if (!f)
        return -1;
    int ok = fputs(val, f) >= 0;
    return (fclose(f) == 0 && ok) ? 0 : -1;
}

static void attr_path(char *out, size_t n, const char *rel)
{
    snprintf(out, n, "%s/%s", iio_dir, rel);
}

static int find_device(void)
{
    DIR *d = opendir("/sys/bus/iio/devices");
    struct dirent *e;
    if (!d)
        return -1;
    while ((e = readdir(d))) {
        char path[600], name[64];
        if (strncmp(e->d_name, "iio:device", 10))
            continue;
        snprintf(path, sizeof path, "/sys/bus/iio/devices/%s/name", e->d_name);
        if (read_attr(path, name, sizeof name) == 0 && !strcmp(name, opt.device)) {
            snprintf(iio_dir, sizeof iio_dir, "/sys/bus/iio/devices/%s", e->d_name);
            int n = atoi(e->d_name + 10);
            closedir(d);
            return n;
        }
    }
    closedir(d);
    return -1;
}

/* Map the DMA blocks and queue them all for capture. 0, or -1 if the kernel
 * doesn't have the block interface (then read() is used). */
static int blocks_start(void)
{
    struct block_alloc_req req = {0, (uint32_t)(opt.samples * 4), (uint32_t)opt.slots, 0};
    if (ioctl(iio_fd, BLOCK_ALLOC_IOCTL, &req) < 0) {
        fprintf(stderr, "iqstream: no DMA block interface (%s): reading instead\n", strerror(errno));
        return -1;
    }
    if ((int)req.count < opt.slots)
        opt.slots = (int)req.count;
    for (int i = 0; i < opt.slots; i++) {
        struct block *b = &ring[i].blk;
        memset(b, 0, sizeof *b);
        b->id = (uint32_t)i;
        if (ioctl(iio_fd, BLOCK_QUERY_IOCTL, b) < 0)
            die("querying DMA block %d: %s", i, strerror(errno));
        void *m = mmap(NULL, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, iio_fd, b->offset);
        if (m == MAP_FAILED)
            die("mapping DMA block %d: %s", i, strerror(errno));
        ring[i].raw = m;
        ring[i].state = SENDING;              /* owned by the DMA until dequeued */
        if (ioctl(iio_fd, BLOCK_ENQUEUE_IOCTL, b) < 0)
            die("queueing DMA block %d: %s", i, strerror(errno));
    }
    return 0;
}

static void iio_start(void)
{
    char path[600], val[64];
    int n = find_device();
    if (n < 0)
        die("no IIO device named %s", opt.device);
    attr_path(path, sizeof path, "buffer/enable");
    if (read_attr(path, val, sizeof val) == 0 && !strcmp(val, "1"))
        die("%s is already streaming (to iiod?): stop that first", opt.device);

    /* Only RX1 I and Q. */
    char dir[600];
    attr_path(dir, sizeof dir, "scan_elements");
    DIR *d = opendir(dir);
    struct dirent *e;
    if (!d)
        die("no %s", dir);
    while ((e = readdir(d))) {
        size_t len = strlen(e->d_name);
        if (len > 3 && !strcmp(e->d_name + len - 3, "_en")) {
            snprintf(path, sizeof path, "%s/%s", dir, e->d_name);
            write_attr(path, "0");
        }
    }
    closedir(d);
    attr_path(path, sizeof path, "scan_elements/in_voltage0_en");
    if (write_attr(path, "1"))
        die("can't enable %s", path);
    attr_path(path, sizeof path, "scan_elements/in_voltage1_en");
    if (write_attr(path, "1"))
        die("can't enable %s", path);

    /* 12-bit packing only if the samples really are 12 bits in 16. */
    attr_path(path, sizeof path, "scan_elements/in_voltage0_type");
    if (read_attr(path, val, sizeof val) == 0) {
        fprintf(stderr, "iqstream: sample format %s\n", val);
        if (opt.packed && strcmp(val, "le:s12/16>>0") && strcmp(val, "le:S12/16>>0")) {
            fprintf(stderr, "iqstream: not 12-bit samples: sending 16-bit instead\n");
            opt.packed = 0;
        }
    }

    /* Block mode: buffer/length is one block; read mode: a kernel buffer of 4 chunks. */
    snprintf(val, sizeof val, "%zu", opt.samples);
    attr_path(path, sizeof path, "buffer/length");
    if (write_attr(path, val))
        die("can't set %s", path);
    snprintf(path, sizeof path, "/dev/iio:device%d", n);
    iio_fd = open(path, O_RDWR | O_NONBLOCK);
    if (iio_fd < 0)
        die("can't open %s: %s", path, strerror(errno));
    if (opt.mmap_mode && blocks_start())
        opt.mmap_mode = 0;
    if (!opt.mmap_mode) {
        snprintf(val, sizeof val, "%zu", opt.samples * 4);
        attr_path(path, sizeof path, "buffer/length");
        write_attr(path, val);
    }
    attr_path(path, sizeof path, "buffer/enable");
    if (write_attr(path, "1"))
        die("can't enable the buffer (%s)", strerror(errno));
    fprintf(stderr, "iqstream: capturing with %s\n", opt.mmap_mode ? "DMA blocks (no copy)" : "read()");
}

static void iio_stop(void)
{
    char path[600];
    if (iio_dir[0]) {
        attr_path(path, sizeof path, "buffer/enable");
        write_attr(path, "0");
    }
    if (iio_fd >= 0) {
        if (opt.mmap_mode)
            ioctl(iio_fd, BLOCK_FREE_IOCTL, 0);
        close(iio_fd);
    }
    iio_fd = -1;
}

/* Wait until the device has data (or 1 s passes). */
static int wait_readable(void)
{
    struct pollfd pfd = {iio_fd, POLLIN, 0};
    int r = poll(&pfd, 1, 1000);
    return r < 0 && errno != EINTR ? -1 : 0;
}

static int read_full(void *buf, size_t n)
{
    uint8_t *p = buf;
    while (n && running) {
        ssize_t r = read(iio_fd, p, n);
        if (r < 0 && (errno == EAGAIN || errno == EINTR)) {
            if (wait_readable())
                return -1;
            continue;
        }
        if (r <= 0)
            return -1;
        p += r;
        n -= (size_t)r;
    }
    return n ? -1 : 0;
}

/* ------------------------------------------------------------ samples */

/* A known pattern for -t: sample k is I = (7k mod 4096) - 2048,
 * Q = ((13k + 1000) mod 4096) - 2048. */
static void test_fill(uint32_t *dst, size_t n, uint64_t *k)
{
    for (size_t j = 0; j < n; j++, (*k)++) {
        uint16_t i = (uint16_t)(((*k * 7) & 0xFFF) - 2048);
        uint16_t q = (uint16_t)(((*k * 13 + 1000) & 0xFFF) - 2048);
        dst[j] = (uint32_t)i | (uint32_t)q << 16;
    }
}

/* 2 x 12 bits -> 3 bytes per sample, from one 32-bit load per sample.
 * Returns how many values didn't fit 12 bits. */
static uint64_t pack12(const uint32_t *in, uint8_t *out, size_t n)
{
    uint64_t bad = 0;
    for (size_t j = 0; j < n; j++) {
        uint32_t v = in[j];
        uint32_t i = v & 0xFFFF, q = v >> 16;
        bad += (((i + 0x800) & 0xF000) != 0) + (((q + 0x800) & 0xF000) != 0);
        uint32_t w = (i & 0xFFF) | (q & 0xFFF) << 12;
        out[0] = (uint8_t)w;
        out[1] = (uint8_t)(w >> 8);
        out[2] = (uint8_t)(w >> 16);
        out += 3;
    }
    return bad;
}

/* ------------------------------------------------------------ threads */

static void publish(struct slot *s, uint32_t seq, uint32_t lost, uint32_t samples)
{
    s->seq = seq;
    s->lost = lost;
    s->samples = samples;
    s->t_ns = now_ns();
    pthread_mutex_lock(&mu);
    s->state = FILLED;
    pthread_cond_signal(&filled);
    pthread_mutex_unlock(&mu);
}

static void *reader(void *arg)
{
    (void)arg;
    size_t bytes = opt.samples * 4;
    uint32_t *scratch = malloc(bytes), seq = 0, lost = 0;
    uint64_t k = 0, produced = 0, start = now_ns();
    if (!scratch)
        die("out of memory");
    tids[0] = (pid_t)syscall(SYS_gettid);
    pin(opt.cpu_reader);
    while (running) {
        if (opt.mmap_mode) {                    /* a DMA block is ready: hand it on */
            struct block b;
            memset(&b, 0, sizeof b);
            if (ioctl(iio_fd, BLOCK_DEQUEUE_IOCTL, &b) < 0) {
                if (errno == EAGAIN || errno == EINTR) {
                    if (wait_readable())
                        break;
                    continue;
                }
                fprintf(stderr, "iqstream: taking a DMA block failed: %s\n", strerror(errno));
                break;
            }
            ring[b.id].blk = b;
            publish(&ring[b.id], seq++, 0, b.bytes_used / 4);
            continue;
        }
        struct slot *s = NULL;
        pthread_mutex_lock(&mu);
        for (int i = 0; i < opt.slots; i++)
            if (ring[i].state == FREE) {
                s = &ring[i];
                break;
            }
        pthread_mutex_unlock(&mu);
        uint32_t *dst = s ? s->raw : scratch;
        if (opt.test) {
            if (opt.test_msps > 0) {            /* pace like the radio would */
                uint64_t due = start + (uint64_t)(produced * 1e3 / opt.test_msps);
                uint64_t now = now_ns();
                if (due > now) {
                    struct timespec ts = {(time_t)((due - now) / 1000000000u),
                                          (long)((due - now) % 1000000000u)};
                    nanosleep(&ts, NULL);
                }
            }
            test_fill(dst, opt.samples, &k);
            produced += opt.samples;
        } else if (read_full(dst, bytes)) {
            if (running)
                fprintf(stderr, "iqstream: reading %s failed: %s\n", opt.device, strerror(errno));
            break;
        }
        if (!s) {                               /* ring full: senders can't keep up */
            lost++;
            dropped_chunks++;
            continue;
        }
        publish(s, seq++, lost, (uint32_t)opt.samples);
        lost = 0;
    }
    running = 0;
    pthread_mutex_lock(&mu);
    pthread_cond_broadcast(&filled);
    pthread_mutex_unlock(&mu);
    free(scratch);
    return NULL;
}

static struct slot *oldest_filled(void)
{
    struct slot *best = NULL;
    for (int i = 0; i < opt.slots; i++)
        if (ring[i].state == FILLED && (!best || ring[i].seq < best->seq))
            best = &ring[i];
    return best;
}

static int send_all(int fd, const void *buf, size_t n)
{
    const uint8_t *p = buf;
    while (n) {
        ssize_t r = send(fd, p, n, MSG_NOSIGNAL);
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

/* Done with a slot: back to the DMA (mmap mode) or to the reader (read mode). */
static void release(struct slot *s)
{
    if (opt.mmap_mode) {
        s->state = SENDING;                     /* the DMA's again until dequeued */
        if (ioctl(iio_fd, BLOCK_ENQUEUE_IOCTL, &s->blk) < 0 && running)
            fprintf(stderr, "iqstream: requeueing a DMA block failed: %s\n", strerror(errno));
        return;
    }
    pthread_mutex_lock(&mu);
    s->state = FREE;
    pthread_mutex_unlock(&mu);
}

struct sender_arg {
    int fd, index;
};

static void *sender(void *p)
{
    struct sender_arg *a = p;
    uint8_t *out = malloc(opt.samples * 3);
    uint32_t *copy = opt.copy_first ? malloc(opt.samples * 4) : NULL;
    if (!out || (opt.copy_first && !copy))
        die("out of memory");
    tids[1 + a->index] = (pid_t)syscall(SYS_gettid);
    pin(opt.cpu_sender[a->index]);
    for (;;) {
        struct slot *s = NULL;
        pthread_mutex_lock(&mu);
        while (running && !(s = oldest_filled()))
            pthread_cond_wait(&filled, &mu);
        if (!s) {
            pthread_mutex_unlock(&mu);
            break;
        }
        s->state = SENDING;
        pthread_mutex_unlock(&mu);

        struct header h = {MAGIC, s->seq, s->samples,
                           (opt.packed ? FLAG_PACKED12 : 0) | (opt.test ? FLAG_TEST : 0), s->lost, s->t_ns};
        int ok;
        if (opt.packed) {
            const uint32_t *src = s->raw;
            if (copy) {                         /* DMA memory may be uncached: one burst copy */
                memcpy(copy, s->raw, (size_t)s->samples * 4);
                src = copy;
            }
            __atomic_add_fetch(&overrange, pack12(src, out, s->samples), __ATOMIC_RELAXED);
            release(s);                         /* packed: the block can go back already */
            ok = !send_all(a->fd, &h, sizeof h) && !send_all(a->fd, out, (size_t)s->samples * 3);
        } else {                                /* 16-bit: straight from the block, like iiod */
            ok = !send_all(a->fd, &h, sizeof h) && !send_all(a->fd, s->raw, (size_t)s->samples * 4);
            release(s);
        }
        if (!ok) {                              /* the receiver went away: stop */
            pthread_mutex_lock(&mu);
            running = 0;
            pthread_cond_broadcast(&filled);
            pthread_mutex_unlock(&mu);
            break;
        }
        __atomic_add_fetch(&sent_chunks, 1, __ATOMIC_RELAXED);
    }
    free(out);
    free(copy);
    return NULL;
}

/* ------------------------------------------------------------ main */

static void on_signal(int sig)
{
    (void)sig;
    running = 0;
}

static void report(const char *when, double secs, long *ticks0)
{
    char cpu[160] = "";
    static const char *names[] = {"reader", "sender0", "sender1"};
    long hz = sysconf(_SC_CLK_TCK);
    for (int i = 0; i < 1 + opt.conns; i++) {
        long t = tids[i] ? thread_ticks(tids[i]) : -1;
        if (t >= 0 && secs > 0) {
            size_t len = strlen(cpu);
            snprintf(cpu + len, sizeof cpu - len, "%s%s %.0f%%", len ? ", " : "", names[i],
                     100.0 * (double)(t - ticks0[i]) / (double)hz / secs);
            ticks0[i] = t;
        }
    }
    fprintf(stderr, "iqstream: %s: %llu chunks sent, %llu dropped, %llu values over 12 bits; cpu: %s\n",
            when, (unsigned long long)sent_chunks, (unsigned long long)dropped_chunks,
            (unsigned long long)overrange, cpu);
}

static void usage(void)
{
    fprintf(stderr,
            "iqstream " VERSION ": stream the Pluto's RX samples over TCP, packed to 12 bits\n"
            "  -p port (5300)  -n connections, 1-2 (2)  -b samples per chunk (131072)\n"
            "  -s chunks in flight (8)  -f 12|16 (12)  -m mmap|read: capture (mmap)\n"
            "  -k copy each DMA block to cached memory before packing\n"
            "  -c reader,sender0,sender1 cores (0,1,0; -1 = any)\n"
            "  -t test pattern instead of the radio  -r test pace in MSPS (0 = flat out)\n"
            "  -d IIO device (cf-ad9361-lpc)\n");
    exit(2);
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "p:n:b:s:f:m:kc:tr:d:h")) != -1) {
        switch (c) {
        case 'p': opt.port = atoi(optarg); break;
        case 'n': opt.conns = atoi(optarg); break;
        case 'b': opt.samples = (size_t)atol(optarg); break;
        case 's': opt.slots = atoi(optarg); break;
        case 'f': opt.packed = atoi(optarg) == 12; break;
        case 'm': opt.mmap_mode = strcmp(optarg, "read") != 0; break;
        case 'k': opt.copy_first = 1; break;
        case 'c':
            if (sscanf(optarg, "%d,%d,%d", &opt.cpu_reader, &opt.cpu_sender[0], &opt.cpu_sender[1]) < 1)
                usage();
            break;
        case 't': opt.test = 1; break;
        case 'r': opt.test_msps = atof(optarg); break;
        case 'd': opt.device = optarg; break;
        default: usage();
        }
    }
    if (opt.conns < 1 || opt.conns > MAX_CONNS || opt.samples < 1024 || opt.slots < 2 ||
        opt.slots > MAX_SLOTS)
        usage();
    if (opt.test)
        opt.mmap_mode = 0;                      /* the test pattern comes from the ring */

    signal(SIGPIPE, SIG_IGN);
    struct sigaction sa = {0};
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    int srv = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)opt.port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (srv < 0 || bind(srv, (struct sockaddr *)&addr, sizeof addr) || listen(srv, MAX_CONNS))
        die("can't listen on port %d: %s", opt.port, strerror(errno));
    fprintf(stderr, "iqstream " VERSION ": port %d, %d connection(s), %zu samples a chunk, "
            "%d chunks, %s%s%s\n", opt.port, opt.conns, opt.samples, opt.slots,
            opt.packed ? "12-bit" : "16-bit", opt.copy_first ? ", copy first" : "",
            opt.test ? ", test pattern" : "");

    int fds[MAX_CONNS];
    for (int i = 0; i < opt.conns; i++) {
        struct pollfd pfd = {srv, POLLIN, 0};
        if (poll(&pfd, 1, 30000) <= 0)
            die("no receiver connected within 30 s");
        fds[i] = accept(srv, NULL, NULL);
        if (fds[i] < 0)
            die("accept: %s", strerror(errno));
    }
    close(srv);

    if (!opt.test)
        iio_start();
    if (!opt.mmap_mode)                         /* read mode: our own ring of chunks */
        for (int i = 0; i < opt.slots; i++) {
            ring[i].raw = malloc(opt.samples * 4);
            ring[i].state = FREE;
            if (!ring[i].raw)
                die("out of memory (%d chunks of %zu samples)", opt.slots, opt.samples);
        }

    pthread_t rt, st[MAX_CONNS];
    struct sender_arg args[MAX_CONNS];
    for (int i = 0; i < opt.conns; i++) {
        args[i].fd = fds[i];
        args[i].index = i;
        pthread_create(&st[i], NULL, sender, &args[i]);
    }
    pthread_create(&rt, NULL, reader, NULL);

    uint64_t t0 = now_ns(), last = t0;
    long ticks5[1 + MAX_CONNS] = {0}, ticks_all[1 + MAX_CONNS] = {0};
    while (running) {
        sleep(1);
        uint64_t now = now_ns();
        if (now - last >= 5000000000u) {
            char when[32];
            snprintf(when, sizeof when, "%.0f s", (now - t0) / 1e9);
            report(when, (now - last) / 1e9, ticks5);
            last = now;
        }
    }
    double total = (now_ns() - t0) / 1e9;
    pthread_mutex_lock(&mu);
    pthread_cond_broadcast(&filled);
    pthread_mutex_unlock(&mu);
    for (int i = 0; i < opt.conns; i++)
        shutdown(fds[i], SHUT_RDWR);
    report("done", total, ticks_all);
    pthread_join(rt, NULL);
    for (int i = 0; i < opt.conns; i++) {
        pthread_join(st[i], NULL);
        close(fds[i]);
    }
    iio_stop();
    return 0;
}
