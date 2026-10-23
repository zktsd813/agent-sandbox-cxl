/* MPTCP protocol/path validation in the existing shared-probe executable.
 * This does not implement microVM device migration or a CXL packet queue. */
#ifndef LAB_MPTCP_PROBE_H
#define LAB_MPTCP_PROBE_H
#include <linux/genetlink.h>
#include <linux/netlink.h>
#include "../linux/include/uapi/linux/mptcp.h"

#ifndef SOL_MPTCP
#define SOL_MPTCP 284
#endif
#ifndef IPPROTO_MPTCP
#define IPPROTO_MPTCP 262
#endif

#define MPTCP_PROBE_PAYLOAD 1024
#define MPTCP_PROBE_MAX_PATHS 8
struct mptcp_probe_record {
    uint64_t schema, nonce, sequence, sent_ns;
    uint32_t length, checksum;
    unsigned char payload[MPTCP_PROBE_PAYLOAD];
};
/* glibc's tcp_info ends at total_retrans; append the Linux UAPI fields through
 * bytes_retrans. The assertions protect the ABI offsets used by Linux 6.6. */
struct mptcp_probe_tcp_info {
    struct tcp_info base;
    uint64_t pacing_rate, max_pacing_rate, bytes_acked, bytes_received;
    uint32_t segs_out, segs_in, notsent_bytes, min_rtt, data_segs_in, data_segs_out;
    uint64_t delivery_rate, busy_time, rwnd_limited, sndbuf_limited;
    uint32_t delivered, delivered_ce;
    uint64_t bytes_sent, bytes_retrans;
};
_Static_assert(offsetof(struct mptcp_probe_tcp_info, bytes_received) == 128,
    "TCP_INFO bytes_received ABI offset changed");
_Static_assert(offsetof(struct mptcp_probe_tcp_info, bytes_sent) == 200,
    "TCP_INFO bytes_sent ABI offset changed");
struct mptcp_probe_paths {
    unsigned count;
    struct mptcp_subflow_info paths[MPTCP_PROBE_MAX_PATHS];
    struct mptcp_probe_tcp_info tcp[MPTCP_PROBE_MAX_PATHS];
};
struct mptcp_probe_observer {
    uint64_t cookie;
    uint32_t token;
    int token_seen, paths_seen;
    struct mptcp_probe_paths previous;
};

static uint64_t mptcp_probe_number(const char *text, unsigned base, uint64_t max)
{
    char *end;
    errno = 0;
    unsigned long long value = strtoull(text, &end, base);
    if (!*text || *text == '-' || *text == '+' || *end || errno || value > max) {
        fprintf(stderr, "Invalid MPTCP numeric argument: %s\n", text);
        exit(2);
    }
    return value;
}

static void mptcp_probe_ipv4(const char *text, struct in_addr *address)
{
    if (inet_pton(AF_INET, text, address) != 1) {
        fprintf(stderr, "MPTCP probe requires an IPv4 address: %s\n", text);
        exit(2);
    }
}

static uint64_t mptcp_probe_cookie(int fd)
{
    uint64_t cookie;
    socklen_t length = sizeof(cookie);
    if (getsockopt(fd, SOL_SOCKET, SO_COOKIE, &cookie, &length) || length != sizeof(cookie))
        die("MPTCP SO_COOKIE");
    return cookie;
}

/* One snapshot every 50ms, with JSON only on address/state changes. This intentionally
 * measures stream continuity, not individual MP_JOIN handshake microseconds. */
static void mptcp_probe_snapshot(int fd, int multipath, char role, const char *side,
                                const char *event, uint64_t round,
                                struct mptcp_probe_observer *observer, int force)
{
    if (mptcp_probe_cookie(fd) != observer->cookie) {
        fprintf(stderr, "MPTCP socket cookie changed\n"); exit(3);
    }
    struct mptcp_info info = {0};
    struct mptcp_probe_paths paths = {0};
    if (multipath) {
        /* Read addresses and TCP_INFO in a single kernel socket lock: separate
         * SUBFLOW_ADDRS/TCPINFO calls can disagree when a subflow is removed. */
        struct mptcp_full_info full = {
            .size_tcpinfo_user = sizeof(paths.tcp[0]),
            .size_sfinfo_user = sizeof(paths.paths[0]),
            .size_arrays_user = MPTCP_PROBE_MAX_PATHS,
            .subflow_info = (uintptr_t)paths.paths,
            .tcp_info = (uintptr_t)paths.tcp,
        };
        socklen_t length = sizeof(full);
        if (getsockopt(fd, SOL_MPTCP, MPTCP_FULL_INFO, &full, &length)) die("MPTCP_FULL_INFO");
        info = full.mptcp_info;
        if (length < offsetof(struct mptcp_full_info, mptcp_info) +
                offsetof(struct mptcp_info, mptcpi_flags) + sizeof(info.mptcpi_flags) ||
            (info.mptcpi_flags & MPTCP_INFO_FLAG_FALLBACK) ||
            !(info.mptcpi_flags & MPTCP_INFO_FLAG_REMOTE_KEY_RECEIVED)) {
            fprintf(stderr, "MPTCP negotiation incomplete or fell back to TCP\n"); exit(3);
        }
        if (observer->token_seen && info.mptcpi_token != observer->token) {
            fprintf(stderr, "MPTCP token changed\n"); exit(3);
        }
        observer->token = info.mptcpi_token;
        observer->token_seen = 1;
        if (full.num_subflows > MPTCP_PROBE_MAX_PATHS ||
            full.size_sfinfo_user != sizeof(paths.paths[0]) ||
            full.size_tcpinfo_user != sizeof(paths.tcp[0])) {
            fprintf(stderr, "MPTCP full subflow response truncated\n"); exit(3);
        }
        paths.count = full.num_subflows;
    }
    int changed = !observer->paths_seen || paths.count != observer->previous.count;
    unsigned established = 0;
    for (unsigned i = 0; i < paths.count; i++) {
        if (paths.tcp[i].base.tcpi_state == TCP_ESTABLISHED) established++;
        if (memcmp(&paths.paths[i], &observer->previous.paths[i], sizeof(paths.paths[i])) ||
            paths.tcp[i].base.tcpi_state != observer->previous.tcp[i].base.tcpi_state)
            changed = 1;
    }
    if (!force && !changed) return;
    observer->previous = paths;
    observer->paths_seen = 1;
    printf("MPTCP_JSON {\"event\":\"%s\",\"role\":\"%c\",\"side\":\"%s\","
        "\"transport\":\"%s\",\"round\":%" PRIu64 ",\"monotonic_ns\":%" PRIu64
        ",\"fd\":%d,\"socket_cookie\":%" PRIu64 ",\"pid\":%d,\"token\":%u,"
        "\"fallback\":false,\"mptcp_flags\":%u,\"additional_subflows\":%u,"
        "\"total_subflows\":%u,\"established_subflows\":%u,\"bytes_sent\":%" PRIu64 ",\"bytes_received\":%" PRIu64
        ",\"bytes_retrans\":%" PRIu64 ",\"paths\":[",
        event, role, side, multipath ? "mptcp" : "tcp", round, monotonic_ns(), fd,
        observer->cookie, getpid(), info.mptcpi_token, info.mptcpi_flags,
        info.mptcpi_subflows, paths.count, established,
        (uint64_t)info.mptcpi_bytes_sent, (uint64_t)info.mptcpi_bytes_received,
        (uint64_t)info.mptcpi_bytes_retrans);
    for (unsigned i = 0; i < paths.count; i++) {
        const struct mptcp_subflow_addrs *path = &paths.paths[i].addrs;
        char local[INET_ADDRSTRLEN], remote[INET_ADDRSTRLEN];
        if (path->sin_local.sin_family != AF_INET || path->sin_remote.sin_family != AF_INET ||
            !inet_ntop(AF_INET, &path->sin_local.sin_addr, local, sizeof(local)) ||
            !inet_ntop(AF_INET, &path->sin_remote.sin_addr, remote, sizeof(remote))) {
            fprintf(stderr, "MPTCP probe received non-IPv4 subflow\n"); exit(3);
        }
        printf("%s{\"local_ip\":\"%s\",\"local_port\":%u,\"remote_ip\":\"%s\",\"remote_port\":%u,"
            "\"subflow_id\":%u,\"tcp_state\":%u,\"bytes_sent\":%" PRIu64
            ",\"bytes_received\":%" PRIu64 ",\"bytes_retrans\":%" PRIu64 "}",
            i ? "," : "", local, ntohs(path->sin_local.sin_port), remote,
            ntohs(path->sin_remote.sin_port), paths.paths[i].id, paths.tcp[i].base.tcpi_state,
            paths.tcp[i].bytes_sent, paths.tcp[i].bytes_received, paths.tcp[i].bytes_retrans);
    }
    printf("],\"errors\":0}\n");
}

static int mptcp_probe_socket(int server, int multipath, char role, const char *peer, unsigned port,
                              int *listener)
{
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, multipath ? IPPROTO_MPTCP : IPPROTO_TCP);
    if (fd < 0) die("MPTCP stream socket");
    int one = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one))) die("MPTCP reuseaddr");
    struct timeval timeout = {.tv_sec = 30};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout))) die("MPTCP timeout");
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_port = htons(port)};
    const char *local = getenv("MPTCP_LOCAL_IP");
    if (server) {
        address.sin_addr.s_addr = htonl(INADDR_ANY);
        if (local && *local) mptcp_probe_ipv4(local, &address.sin_addr);
        if (bind(fd, (void *)&address, sizeof(address)) || listen(fd, 1)) die("MPTCP stream listen");
        printf("MPTCP_JSON {\"event\":\"listening\",\"role\":\"%c\",\"transport\":\"%s\",\"port\":%u}\n",
            role, multipath ? "mptcp" : "tcp", port);
        int accepted = accept4(fd, NULL, NULL, SOCK_CLOEXEC);
        if (accepted < 0) die("MPTCP stream accept");
        /* Additional MP_JOIN SYNs use the listening port before the kernel can
         * find their existing MPTCP token. Keep it open, but accept only once. */
        *listener = fd;
        fd = accepted;
    } else {
        if (local && *local) {
            struct sockaddr_in source = {.sin_family = AF_INET};
            mptcp_probe_ipv4(local, &source.sin_addr);
            if (bind(fd, (void *)&source, sizeof(source))) die("MPTCP source bind");
        }
        mptcp_probe_ipv4(peer, &address.sin_addr);
        /* A single attempt: no reconnect can accidentally pass continuity. */
        if (connect(fd, (void *)&address, sizeof(address))) die("MPTCP stream connect");
    }
    if (setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one))) die("MPTCP nodelay");
    return fd;
}

static int mptcp_probe_compare_u64(const void *a, const void *b)
{
    uint64_t av = *(const uint64_t *)a, bv = *(const uint64_t *)b;
    return (av > bv) - (av < bv);
}

static void mptcp_probe_stream(char role, const char *side, const char *peer,
                               unsigned port, unsigned rounds, unsigned interval_us, uint64_t nonce)
{
    const char *transport = getenv("MPTCP_PROTOCOL");
    if (transport && strcmp(transport, "tcp") && strcmp(transport, "mptcp")) {
        fprintf(stderr, "MPTCP_PROTOCOL must be tcp or mptcp\n"); exit(2);
    }
    int multipath = !transport || !strcmp(transport, "mptcp");
    int server = !strcmp(side, "server");
    setvbuf(stdout, NULL, _IONBF, 0);
    int listener = -1;
    int fd = mptcp_probe_socket(server, multipath, role, peer, port, &listener);
    struct mptcp_probe_observer observer = {.cookie = mptcp_probe_cookie(fd)};
    uint64_t *rtts = calloc(rounds, sizeof(*rtts));
    if (!rtts) die("MPTCP RTT allocation");
    uint64_t begin = monotonic_ns(), previous = begin, max_gap = 0, sum = 0, next_snapshot = 0;
    struct rusage cpu_begin, cpu_end;
    if (getrusage(RUSAGE_SELF, &cpu_begin)) die("MPTCP rusage start");
    for (unsigned round = 0; round < rounds; round++) {
        struct mptcp_probe_record record = {0}, echo;
        uint64_t send_start = monotonic_ns();
        if (!server) {
            record.schema = UINT64_C(0x4d50544350535401);
            record.nonce = nonce;
            record.sequence = round;
            record.sent_ns = send_start;
            record.length = sizeof(record.payload);
            for (size_t i = 0; i < sizeof(record.payload); i++)
                record.payload[i] = pattern(nonce, round, 0, i);
            record.checksum = checksum(record.payload, sizeof(record.payload));
            migration_io(fd, &record, sizeof(record), 1);
            migration_io(fd, &echo, sizeof(echo), 0);
            if (memcmp(&record, &echo, sizeof(record))) {
                fprintf(stderr, "MPTCP echo/sequence/payload mismatch\n"); exit(3);
            }
        } else {
            migration_io(fd, &record, sizeof(record), 0);
            if (record.schema != UINT64_C(0x4d50544350535401) || record.nonce != nonce ||
                record.sequence != round || record.length != sizeof(record.payload) ||
                record.checksum != checksum(record.payload, sizeof(record.payload))) {
                fprintf(stderr, "MPTCP stream sequence/checksum mismatch\n"); exit(3);
            }
            for (size_t i = 0; i < sizeof(record.payload); i++) {
                if (record.payload[i] != pattern(nonce, round, 0, i)) {
                    fprintf(stderr, "MPTCP stream payload mismatch\n"); exit(3);
                }
            }
            migration_io(fd, &record, sizeof(record), 1);
        }
        uint64_t now = monotonic_ns(), gap = now - previous;
        if (round && gap > max_gap) max_gap = gap;
        previous = now;
        rtts[round] = now - send_start;
        sum += rtts[round];
        if (!round || now >= next_snapshot) {
            mptcp_probe_snapshot(fd, multipath, role, side, round ? "paths" : "ready", round,
                &observer, !round);
            next_snapshot = now + UINT64_C(50000000);
        }
        if (!server && interval_us && round + 1 < rounds) {
            struct timespec pause = {.tv_sec = interval_us / 1000000,
                .tv_nsec = (long)(interval_us % 1000000) * 1000};
            while (nanosleep(&pause, &pause) && errno == EINTR) {}
        }
    }
    uint64_t elapsed = monotonic_ns() - begin;
    if (getrusage(RUSAGE_SELF, &cpu_end)) die("MPTCP rusage end");
    mptcp_probe_snapshot(fd, multipath, role, side, "final_paths", rounds, &observer, 1);
    /* Both sides snapshot their live paths before either closes its socket.
     * Otherwise a successful last echo can race a peer FIN and look inactive. */
    uint64_t completion = nonce ^ UINT64_C(0x4d50544350444f4e), received_completion;
    if (server) {
        migration_io(fd, &received_completion, sizeof(received_completion), 0);
        if (received_completion != completion) { fprintf(stderr, "MPTCP completion mismatch\n"); exit(3); }
        migration_io(fd, &completion, sizeof(completion), 1);
    } else {
        migration_io(fd, &completion, sizeof(completion), 1);
        migration_io(fd, &received_completion, sizeof(received_completion), 0);
        if (received_completion != completion) { fprintf(stderr, "MPTCP completion mismatch\n"); exit(3); }
    }
    qsort(rtts, rounds, sizeof(*rtts), mptcp_probe_compare_u64);
    printf("MPTCP_JSON {\"event\":\"complete\",\"role\":\"%c\",\"side\":\"%s\",\"transport\":\"%s\","
        "\"nonce\":\"%016" PRIx64 "\",\"rounds\":%u,\"payload_bytes_per_round\":%u,"
        "\"application_payload_bytes_each_direction\":%" PRIu64 ",\"interval_us\":%u,"
        "\"elapsed_ns\":%" PRIu64 ",\"mean_exchange_ns\":%.3f,\"p50_exchange_ns\":%" PRIu64
        ",\"p99_exchange_ns\":%" PRIu64 ",\"max_exchange_ns\":%" PRIu64 ",\"max_completion_gap_ns\":%" PRIu64
        ",\"user_cpu_ns\":%" PRIu64 ",\"system_cpu_ns\":%" PRIu64 ",\"socket_cookie\":%" PRIu64
        ",\"token\":%u,\"connect_attempts\":%u,\"errors\":0}\n",
        role, side, multipath ? "mptcp" : "tcp", nonce, rounds, MPTCP_PROBE_PAYLOAD,
        (uint64_t)rounds * MPTCP_PROBE_PAYLOAD, interval_us, elapsed, (double)sum / rounds,
        rtts[rounds / 2], rtts[((uint64_t)rounds * 99) / 100], rtts[rounds - 1], max_gap,
        tcp_bench_cpu_ns(&cpu_end.ru_utime, &cpu_begin.ru_utime),
        tcp_bench_cpu_ns(&cpu_end.ru_stime, &cpu_begin.ru_stime), observer.cookie,
        observer.token, server ? 0 : 1);
    close(fd);
    if (listener >= 0) close(listener);
    free(rtts);
}

/* Small generic-netlink client avoids adding iproute2 or another executable to
 * either initramfs. Only MPTCP endpoints/limits in this network namespace change. */
struct mptcp_probe_nl_request {
    struct nlmsghdr header;
    struct genlmsghdr generic;
    unsigned char attributes[256];
};

static void mptcp_probe_nl_attr(struct mptcp_probe_nl_request *request,
                                unsigned type, const void *data, size_t length)
{
    size_t offset = NLMSG_ALIGN(request->header.nlmsg_len);
    size_t total = NLA_HDRLEN + length;
    if (offset + NLA_ALIGN(total) > sizeof(*request)) {
        fprintf(stderr, "MPTCP netlink request overflow\n"); exit(2);
    }
    struct nlattr *attribute = (void *)((unsigned char *)request + offset);
    attribute->nla_type = type;
    attribute->nla_len = total;
    if (length) memcpy((unsigned char *)attribute + NLA_HDRLEN, data, length);
    request->header.nlmsg_len = offset + NLA_ALIGN(total);
}

static void mptcp_probe_nl_init(struct mptcp_probe_nl_request *request, unsigned family,
                                unsigned command, unsigned sequence)
{
    memset(request, 0, sizeof(*request));
    request->header.nlmsg_len = NLMSG_LENGTH(GENL_HDRLEN);
    request->header.nlmsg_type = family;
    request->header.nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    request->header.nlmsg_seq = sequence;
    request->generic.cmd = command;
    request->generic.version = MPTCP_PM_VER;
}

static uint16_t mptcp_probe_nl_exchange(int fd, struct mptcp_probe_nl_request *request, int family_query)
{
    struct sockaddr_nl kernel = {.nl_family = AF_NETLINK};
    if (sendto(fd, request, request->header.nlmsg_len, 0, (void *)&kernel, sizeof(kernel)) !=
        (ssize_t)request->header.nlmsg_len) die("MPTCP netlink send");
    uint16_t family = 0;
    for (;;) {
        union { uint64_t alignment; unsigned char bytes[4096]; } response;
        struct sockaddr_nl sender = {0};
        socklen_t sender_length = sizeof(sender);
        ssize_t received = recvfrom(fd, response.bytes, sizeof(response.bytes), 0,
            (void *)&sender, &sender_length);
        if (received < 0 && errno == EINTR) continue;
        if (received <= 0) die("MPTCP netlink receive");
        if (sender.nl_pid) { fprintf(stderr, "MPTCP netlink response not from kernel\n"); exit(3); }
        int remaining = received;
        for (struct nlmsghdr *message = (void *)response.bytes; NLMSG_OK(message, remaining);
             message = NLMSG_NEXT(message, remaining)) {
            if (message->nlmsg_seq != request->header.nlmsg_seq) continue;
            if (message->nlmsg_type == NLMSG_ERROR) {
                if (message->nlmsg_len < NLMSG_LENGTH(sizeof(struct nlmsgerr))) {
                    fprintf(stderr, "MPTCP netlink short error\n"); exit(3);
                }
                struct nlmsgerr *error = NLMSG_DATA(message);
                if (error->error) { errno = -error->error; die("MPTCP netlink command"); }
                if (!family_query || family) return family;
            } else if (family_query && message->nlmsg_type == GENL_ID_CTRL &&
                       message->nlmsg_len >= NLMSG_LENGTH(GENL_HDRLEN)) {
                size_t available = message->nlmsg_len - NLMSG_LENGTH(GENL_HDRLEN);
                unsigned char *cursor = (unsigned char *)NLMSG_DATA(message) + GENL_HDRLEN;
                while (available >= NLA_HDRLEN) {
                    struct nlattr *attribute = (void *)cursor;
                    if (attribute->nla_len < NLA_HDRLEN || attribute->nla_len > available) break;
                    if ((attribute->nla_type & NLA_TYPE_MASK) == CTRL_ATTR_FAMILY_ID &&
                        attribute->nla_len == NLA_HDRLEN + sizeof(family))
                        memcpy(&family, cursor + NLA_HDRLEN, sizeof(family));
                    size_t step = NLA_ALIGN(attribute->nla_len);
                    if (step > available) break;
                    available -= step;
                    cursor += step;
                }
            }
        }
    }
}

static void mptcp_probe_pm(char role, const char *operation, const char *first,
                           const char *second, const char *flags_text)
{
    uint64_t started = monotonic_ns();
    int fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_GENERIC);
    if (fd < 0) die("MPTCP netlink socket");
    struct timeval timeout = {.tv_sec = 5};
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout))) die("MPTCP netlink timeout");
    struct mptcp_probe_nl_request request;
    mptcp_probe_nl_init(&request, GENL_ID_CTRL, CTRL_CMD_GETFAMILY, 1);
    mptcp_probe_nl_attr(&request, CTRL_ATTR_FAMILY_NAME, MPTCP_PM_NAME, sizeof(MPTCP_PM_NAME));
    uint16_t family = mptcp_probe_nl_exchange(fd, &request, 1);
    if (!family) { fprintf(stderr, "MPTCP netlink family not found\n"); exit(3); }
    if (!strcmp(operation, "limits")) {
        uint32_t subflows = mptcp_probe_number(first, 10, 8);
        uint32_t accepted = mptcp_probe_number(second, 10, 8);
        mptcp_probe_nl_init(&request, family, MPTCP_PM_CMD_SET_LIMITS, 2);
        mptcp_probe_nl_attr(&request, MPTCP_PM_ATTR_SUBFLOWS, &subflows, sizeof(subflows));
        mptcp_probe_nl_attr(&request, MPTCP_PM_ATTR_RCV_ADD_ADDRS, &accepted, sizeof(accepted));
    } else {
        struct in_addr address;
        mptcp_probe_ipv4(first, &address);
        uint8_t id = mptcp_probe_number(second, 10, 255);
        uint16_t af = AF_INET;
        int adding = !strcmp(operation, "add");
        uint32_t flags = 0;
        if (adding) {
            if (!id) { fprintf(stderr, "MPTCP added endpoint ID must be nonzero\n"); exit(2); }
            flags = !strcmp(flags_text, "subflow") ? MPTCP_PM_ADDR_FLAG_SUBFLOW : MPTCP_PM_ADDR_FLAG_SIGNAL;
        }
        mptcp_probe_nl_init(&request, family, adding ? MPTCP_PM_CMD_ADD_ADDR : MPTCP_PM_CMD_DEL_ADDR, 2);
        size_t nested_offset = NLMSG_ALIGN(request.header.nlmsg_len);
        mptcp_probe_nl_attr(&request, MPTCP_PM_ATTR_ADDR | NLA_F_NESTED, NULL, 0);
        mptcp_probe_nl_attr(&request, MPTCP_PM_ADDR_ATTR_FAMILY, &af, sizeof(af));
        mptcp_probe_nl_attr(&request, MPTCP_PM_ADDR_ATTR_ID, &id, sizeof(id));
        mptcp_probe_nl_attr(&request, MPTCP_PM_ADDR_ATTR_ADDR4, &address, sizeof(address));
        if (adding) mptcp_probe_nl_attr(&request, MPTCP_PM_ADDR_ATTR_FLAGS, &flags, sizeof(flags));
        struct nlattr *nested = (void *)((unsigned char *)&request + nested_offset);
        nested->nla_len = request.header.nlmsg_len - nested_offset;
    }
    mptcp_probe_nl_exchange(fd, &request, 0);
    close(fd);
    printf("MPTCP_JSON {\"event\":\"pm\",\"role\":\"%c\",\"operation\":\"%s\","
        "\"argument1\":\"%s\",\"argument2\":\"%s\",\"started_ns\":%" PRIu64
        ",\"elapsed_ns\":%" PRIu64 ",\"errors\":0}\n",
        role, operation, first, second, started, monotonic_ns() - started);
}

static int mptcp_probe_dispatch(int argc, char **argv, char role)
{
    if (!strcmp(argv[1], "mptcp-stream")) {
        if (argc != 9 || (strcmp(argv[3], "server") && strcmp(argv[3], "client"))) {
            fprintf(stderr, "Usage: shared-probe mptcp-stream A|B server|client PEER_IP PORT ROUNDS INTERVAL_US NONCE_HEX\n");
            return 2;
        }
        unsigned port = mptcp_probe_number(argv[5], 10, 65535);
        unsigned rounds = mptcp_probe_number(argv[6], 10, 1000000);
        unsigned interval = mptcp_probe_number(argv[7], 10, 1000000);
        uint64_t nonce = mptcp_probe_number(argv[8], 16, UINT64_MAX);
        if (!port || !rounds || !nonce) return 2;
        struct in_addr peer;
        mptcp_probe_ipv4(argv[4], &peer);
        mptcp_probe_stream(role, argv[3], argv[4], port, rounds, interval, nonce);
        return 0;
    }
    if (argc < 6 || (strcmp(argv[3], "limits") && strcmp(argv[3], "add") && strcmp(argv[3], "delete")) ||
        ((!strcmp(argv[3], "add")) ? (argc != 7 || (strcmp(argv[6], "subflow") && strcmp(argv[6], "signal"))) : argc != 6)) {
        fprintf(stderr, "Usage: shared-probe mptcp-pm A|B limits SUBFLOWS ACCEPTED; add IP ID subflow|signal; delete IP ID\n");
        return 2;
    }
    mptcp_probe_pm(role, argv[3], argv[4], argv[5], argc == 7 ? argv[6] : "");
    return 0;
}
#endif
