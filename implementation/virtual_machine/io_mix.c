/* Lab-only I/O fixture. Runs inside L1/L2, never configures the physical host. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/if_tun.h>
#include <linux/vm_sockets.h>
#include <net/if.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

static void need(int ok, const char *s) { if (!ok) { perror(s); exit(1); } }
static void exact(int fd, void *buffer, size_t bytes, int writing)
{
    unsigned char *p = buffer;
    while (bytes) {
        ssize_t n = writing ? write(fd, p, bytes) : read(fd, p, bytes);
        if (n < 0 && errno == EINTR) continue;
        need(n > 0, "stream transfer"); p += n; bytes -= n;
    }
}
static int sock(int family, int type)
{
    int fd = socket(family, type, 0); need(fd >= 0, "socket");
    struct timeval timeout = {.tv_sec = 20};
    need(!setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)), "timeout");
    return fd;
}
static void net_up(const char *name, const char *ip, int tap)
{
    struct ifreq r = {0};
    need(strlen(name) < IFNAMSIZ, "interface name"); strcpy(r.ifr_name, name);
    if (tap) {
        int fd = open("/dev/net/tun", O_RDWR); need(fd >= 0, "L1 tun");
        r.ifr_flags = IFF_TAP | IFF_NO_PI;
        need(!ioctl(fd, TUNSETIFF, &r) && !ioctl(fd, TUNSETPERSIST, 1), "persistent L1 tap"); close(fd);
    }
    int fd = sock(AF_INET, SOCK_DGRAM);
    struct sockaddr_in address = {.sin_family = AF_INET};
    need(inet_pton(AF_INET, ip, &address.sin_addr) == 1, "IP address");
    memcpy(&r.ifr_addr, &address, sizeof(address)); need(!ioctl(fd, SIOCSIFADDR, &r), "interface address");
    address.sin_addr.s_addr = inet_addr("255.255.255.0");
    memcpy(&r.ifr_netmask, &address, sizeof(address)); need(!ioctl(fd, SIOCSIFNETMASK, &r), "interface mask");
    r.ifr_flags = IFF_UP | IFF_RUNNING; need(!ioctl(fd, SIOCSIFFLAGS, &r), "interface up"); close(fd);
}
static void guest(unsigned epoch)
{
    net_up("eth0", "172.31.0.2", 0);
    int udp = sock(AF_INET, SOCK_DGRAM), vsock = sock(AF_VSOCK, SOCK_STREAM);
    struct sockaddr_in address = {.sin_family=AF_INET, .sin_port=htons(9999), .sin_addr.s_addr=INADDR_ANY};
    struct sockaddr_vm va = {.svm_family=AF_VSOCK, .svm_cid=VMADDR_CID_ANY, .svm_port=8000};
    need(!bind(udp, (void *)&address, sizeof(address)), "UDP bind");
    need(!bind(vsock, (void *)&va, sizeof(va)) && !listen(vsock, 1), "vsock listen");
    const size_t length = 16 * 1024 * 1024;
    volatile unsigned char *ram = malloc(length); need(ram != NULL, "RAM");
    for (size_t i=0; i<length; i++) ram[i] = (unsigned char)(i*13 + epoch);
    void *block; need(!posix_memalign(&block, 4096, 4096), "block buffer");
    int disk = open("/dev/vda", O_RDONLY | O_DIRECT); need(disk >= 0, "guest block");
    for (unsigned i=0; i<256; i++) need(read(disk, block, 4096) == 4096, "direct block read");
    close(disk); free(block);
    puts("IO_MIX_READY"); fflush(stdout);
    for (unsigned packet=0; packet<16; packet++) {
        unsigned char data[1024]; struct sockaddr_in peer; socklen_t size=sizeof(peer);
        need(recvfrom(udp, data, sizeof(data), 0, (void *)&peer, &size) == sizeof(data), "UDP receive");
        for (unsigned i=0; i<sizeof(data); i++) need(data[i] == (unsigned char)(epoch*31+packet+i), "UDP content");
        unsigned char ack=packet; need(sendto(udp,&ack,1,0,(void *)&peer,size)==1,"UDP ack");
    }
    int connection = accept(vsock, NULL, NULL); need(connection >= 0, "vsock accept");
    unsigned char data[65536]; exact(connection,data,sizeof(data),0); uint64_t sum=0;
    for (unsigned i=0;i<sizeof(data);i++) { need(data[i]==(unsigned char)(epoch*17+i),"vsock content"); sum+=data[i]; }
    exact(connection,&sum,sizeof(sum),1); close(connection); close(vsock); close(udp);
    for (size_t i=0;i<length;i++) need(ram[i]==(unsigned char)(i*13+epoch),"retained RAM checksum");
    free((void *)ram);
    printf("IO_MIX_PASS {\"epoch\":%u,\"ram_bytes\":%zu,\"block_bytes\":1048576,\"udp_bytes\":16384,\"vsock_bytes\":65536,\"checksum\":%" PRIu64 "}\n",epoch,length,sum);
}
static void host(unsigned epoch, const char *path)
{
    int udp=sock(AF_INET,SOCK_DGRAM);
    struct sockaddr_in address={.sin_family=AF_INET,.sin_port=htons(9999),.sin_addr.s_addr=inet_addr("172.31.0.2")};
    for (unsigned packet=0;packet<16;packet++) {
        unsigned char data[1024],ack=255;
        for(unsigned i=0;i<sizeof(data);i++)data[i]=(unsigned char)(epoch*31+packet+i);
        need(sendto(udp,data,sizeof(data),0,(void *)&address,sizeof(address))==sizeof(data),"UDP send");
        need(recv(udp,&ack,1,0)==1 && ack==packet,"UDP ack content");
    }
    close(udp);
    int fd=sock(AF_UNIX,SOCK_STREAM); struct sockaddr_un address_un={.sun_family=AF_UNIX};
    need(strlen(path)<sizeof(address_un.sun_path),"UDS path"); strcpy(address_un.sun_path,path);
    need(!connect(fd,(void *)&address_un,sizeof(address_un)),"vsock UDS connect");
    char command[]="CONNECT 8000\n"; exact(fd,command,sizeof(command)-1,1);
    char reply[128]={0}; unsigned n=0;
    do { need(n<sizeof(reply)-1,"vsock handshake size"); exact(fd,&reply[n],1,0); } while(reply[n++]!='\n');
    need(!strncmp(reply,"OK ",3),"vsock handshake");
    unsigned char data[65536]; uint64_t sum=0,ack=0;
    for(unsigned i=0;i<sizeof(data);i++){data[i]=(unsigned char)(epoch*17+i);sum+=data[i];}
    exact(fd,data,sizeof(data),1); exact(fd,&ack,sizeof(ack),0); need(ack==sum,"vsock ack content"); close(fd);
    printf("IO_MIX_HOST_PASS %u\n",epoch);
}
int main(int argc,char **argv)
{
    if(argc==4 && !strcmp(argv[1],"tap"))net_up(argv[2],argv[3],1);
    else if(argc==3 && !strcmp(argv[1],"guest"))guest(strtoul(argv[2],NULL,10));
    else if(argc==4 && !strcmp(argv[1],"host"))host(strtoul(argv[2],NULL,10),argv[3]);
    else { fprintf(stderr,"io-mix tap IF IP | guest EPOCH | host EPOCH UDS\n"); return 2; }
    return 0;
}
