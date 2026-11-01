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
 * One reader thread reads the IIO capture device into a ring of chunks; 1-2
 * sender threads (one TCP connection each) take the oldest chunk, pack it and
 * send it. Chunks are numbered, so the receiver puts them back in order. If
 * the ring is full, a chunk is dropped and counted in the next one's header.
 *
 * Every chunk on the wire: a 28-byte little-endian header, then the samples:
 *   uint32 magic "IQS1", seq (0, 1, 2 ...), samples, flags, lost (chunks dropped
 *   just before this one); uint64 t_ns (CLOCK_REALTIME when it was read)
 *   flags: 1 = packed 12-bit, 3 bytes a sample: b0 = I[7:0],
 *          b1 = I[11:8] | Q[3:0] << 4, b2 = Q[11:4]; else int16 I, int16 Q
 *          2 = test pattern (-t), not the radio
 *
 *   iqstream [-p port] [-n connections] [-b samples per chunk] [-s ring slots]
 *            [-f 12|16] [-c reader,sender0,sender1 cores] [-t] [-r test MSPS]
 *            [-d IIO device name]
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
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define VERSION "1"
#define MAGIC 0x31535149u            /* "IQS1" */
#define F_PACKED12 1u
#define F_TEST 2u
#define MAX_CONNS 2

struct header {
    uint32_t magic, seq, samples, flags, lost;
    uint64_t t_ns;
} __attribute__((packed));

enum { FREE, FILLED, SENDING };
struct slot {
    int state;
    uint32_t seq, lost;
    uint64_t t_ns;
    int16_t *raw;                     /* I, Q, I, Q ... as read */
    uint8_t *out;                     /* packed */
};

static struct {
    int port, conns, slots, packed, test;
    size_t samples;
    double test_msps;
    int cpu_reader, cpu_sender[MAX_CONNS];
    const char *device;
} opt = {5300, 2, 8, 1, 0, 1 << 17, 0.0, 0, {1, 0}, "cf-ad9361-lpc"};
/* Default cores: the Ethernet interrupts land on core 0, so the first sender
 * (the only one with -n 1) runs on core 1; the reader shares core 0. */

static struct slot *ring;
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t filled = PTHREAD_COND_INITIALIZER;
static volatile sig_atomic_t running = 1;
static uint64_t sent_chunks, dropped_chunks, overrange;
static char iio_dir[300];
static int iio_fd = -1;

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

    /* A kernel buffer of 4 chunks. */
    snprintf(val, sizeof val, "%zu", opt.samples * 4);
    attr_path(path, sizeof path, "buffer/length");
    if (write_attr(path, val))
        die("can't set %s", path);
    attr_path(path, sizeof path, "buffer/enable");
    if (write_attr(path, "1"))
        die("can't enable the buffer (%s)", strerror(errno));

    snprintf(path, sizeof path, "/dev/iio:device%d", n);
    iio_fd = open(path, O_RDONLY);
    if (iio_fd < 0)
        die("can't open %s: %s", path, strerror(errno));
}

static void iio_stop(void)
{
    char path[600];
    if (iio_fd >= 0)
        close(iio_fd);
    iio_fd = -1;
    if (iio_dir[0]) {
        attr_path(path, sizeof path, "buffer/enable");
        write_attr(path, "0");
    }
}

static int read_full(int fd, void *buf, size_t n)
{
    uint8_t *p = buf;
    while (n) {
        ssize_t r = read(fd, p, n);
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}

/* ------------------------------------------------------------ samples */

/* A known pattern for -t: sample k is I = (7k mod 4096) - 2048,
 * Q = ((13k + 1000) mod 4096) - 2048. */
static void test_fill(int16_t *dst, size_t n, uint64_t *k)
{
    for (size_t j = 0; j < n; j++, (*k)++) {
        dst[2 * j] = (int16_t)(((*k * 7) & 0xFFF) - 2048);
        dst[2 * j + 1] = (int16_t)(((*k * 13 + 1000) & 0xFFF) - 2048);
    }
}

/* 2 x 12 bits -> 3 bytes per sample. Returns how many values didn't fit 12 bits. */
static uint64_t pack12(const int16_t *in, uint8_t *out, size_t n)
{
    uint64_t bad = 0;
    for (size_t j = 0; j < n; j++) {
        int16_t i = in[2 * j], q = in[2 * j + 1];
        bad += ((uint16_t)(i + 2048) > 4095) + ((uint16_t)(q + 2048) > 4095);
        uint16_t ui = (uint16_t)i & 0xFFF, uq = (uint16_t)q & 0xFFF;
        out[0] = (uint8_t)ui;
        out[1] = (uint8_t)((ui >> 8) | ((uq & 0xF) << 4));
        out[2] = (uint8_t)(uq >> 4);
        out += 3;
    }
    return bad;
}

/* ------------------------------------------------------------ threads */

static void *reader(void *arg)
{
    (void)arg;
    size_t bytes = opt.samples * 4;
    int16_t *scratch = malloc(bytes);
    uint32_t seq = 0, lost = 0;
    uint64_t k = 0, produced = 0, start = now_ns();
    if (!scratch)
        die("out of memory");
    pin(opt.cpu_reader);
    while (running) {
        struct slot *s = NULL;
        pthread_mutex_lock(&mu);
        for (int i = 0; i < opt.slots; i++)
            if (ring[i].state == FREE) {
                s = &ring[i];
                break;
            }
        pthread_mutex_unlock(&mu);
        int16_t *dst = s ? s->raw : scratch;
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
        } else if (read_full(iio_fd, dst, bytes)) {
            fprintf(stderr, "iqstream: reading %s failed: %s\n", opt.device, strerror(errno));
            running = 0;
            break;
        }
        if (!s) {                               /* ring full: senders can't keep up */
            lost++;
            dropped_chunks++;
            continue;
        }
        s->seq = seq++;
        s->lost = lost;
        lost = 0;
        s->t_ns = now_ns();
        pthread_mutex_lock(&mu);
        s->state = FILLED;
        pthread_cond_signal(&filled);
        pthread_mutex_unlock(&mu);
    }
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

struct sender_arg {
    int fd, index;
};

static void *sender(void *p)
{
    struct sender_arg *a = p;
    size_t payload = opt.samples * (opt.packed ? 3 : 4);
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

        const void *data = s->raw;
        if (opt.packed) {
            __atomic_add_fetch(&overrange, pack12(s->raw, s->out, opt.samples), __ATOMIC_RELAXED);
            data = s->out;
        }
        struct header h = {MAGIC, s->seq, (uint32_t)opt.samples,
                           (opt.packed ? F_PACKED12 : 0) | (opt.test ? F_TEST : 0), s->lost, s->t_ns};
        int ok = !send_all(a->fd, &h, sizeof h) && !send_all(a->fd, data, payload);

        pthread_mutex_lock(&mu);
        s->state = FREE;
        if (!ok) {                              /* the receiver went away: stop */
            running = 0;
            pthread_cond_broadcast(&filled);
        }
        pthread_mutex_unlock(&mu);
        if (!ok)
            break;
        __atomic_add_fetch(&sent_chunks, 1, __ATOMIC_RELAXED);
    }
    return NULL;
}

/* ------------------------------------------------------------ main */

static void on_signal(int sig)
{
    (void)sig;
    running = 0;
}

static void usage(void)
{
    fprintf(stderr,
            "iqstream " VERSION ": stream the Pluto's RX samples over TCP, packed to 12 bits\n"
            "  -p port (5300)  -n connections, 1-2 (2)  -b samples per chunk (131072)\n"
            "  -s ring slots (8)  -f 12|16 (12)  -c reader,sender0,sender1 cores (0,1,0; -1 = any)\n"
            "  -t test pattern instead of the radio  -r test pace in MSPS (0 = flat out)\n"
            "  -d IIO device (cf-ad9361-lpc)\n");
    exit(2);
}

int main(int argc, char **argv)
{
    int c;
    while ((c = getopt(argc, argv, "p:n:b:s:f:c:tr:d:h")) != -1) {
        switch (c) {
        case 'p': opt.port = atoi(optarg); break;
        case 'n': opt.conns = atoi(optarg); break;
        case 'b': opt.samples = (size_t)atol(optarg); break;
        case 's': opt.slots = atoi(optarg); break;
        case 'f': opt.packed = atoi(optarg) == 12; break;
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
    if (opt.conns < 1 || opt.conns > MAX_CONNS || opt.samples < 1024 || opt.slots < 2)
        usage();

    signal(SIGPIPE, SIG_IGN);
    struct sigaction sa = {0};
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    ring = calloc((size_t)opt.slots, sizeof *ring);
    if (!ring)
        die("out of memory");
    for (int i = 0; i < opt.slots; i++) {
        ring[i].raw = malloc(opt.samples * 4);
        ring[i].out = malloc(opt.samples * 3);
        if (!ring[i].raw || !ring[i].out)
            die("out of memory (%d slots of %zu samples)", opt.slots, opt.samples);
    }

    int srv = socket(AF_INET, SOCK_STREAM, 0), one = 1;
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)opt.port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (srv < 0 || bind(srv, (struct sockaddr *)&addr, sizeof addr) || listen(srv, MAX_CONNS))
        die("can't listen on port %d: %s", opt.port, strerror(errno));
    fprintf(stderr, "iqstream " VERSION ": port %d, %d connection(s), %zu samples a chunk, "
            "%d slots, %s%s\n", opt.port, opt.conns, opt.samples, opt.slots,
            opt.packed ? "12-bit" : "16-bit", opt.test ? ", test pattern" : "");

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

    pthread_t rt, st[MAX_CONNS];
    struct sender_arg args[MAX_CONNS];
    for (int i = 0; i < opt.conns; i++) {
        args[i].fd = fds[i];
        args[i].index = i;
        pthread_create(&st[i], NULL, sender, &args[i]);
    }
    pthread_create(&rt, NULL, reader, NULL);

    uint64_t t0 = now_ns(), last = t0;
    while (running) {
        sleep(1);
        uint64_t now = now_ns();
        if (now - last >= 5000000000u) {
            last = now;
            fprintf(stderr, "iqstream: %.0f s: %llu chunks sent, %llu dropped, %llu values over 12 bits\n",
                    (now - t0) / 1e9, (unsigned long long)sent_chunks,
                    (unsigned long long)dropped_chunks, (unsigned long long)overrange);
        }
    }
    pthread_mutex_lock(&mu);
    pthread_cond_broadcast(&filled);
    pthread_mutex_unlock(&mu);
    for (int i = 0; i < opt.conns; i++)
        shutdown(fds[i], SHUT_RDWR);
    pthread_join(rt, NULL);
    for (int i = 0; i < opt.conns; i++) {
        pthread_join(st[i], NULL);
        close(fds[i]);
    }
    iio_stop();
    fprintf(stderr, "iqstream: done: %llu chunks sent, %llu dropped, %llu values over 12 bits\n",
            (unsigned long long)sent_chunks, (unsigned long long)dropped_chunks,
            (unsigned long long)overrange);
    return 0;
}
