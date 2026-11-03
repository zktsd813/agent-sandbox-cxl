// SPDX-License-Identifier: Apache-2.0
// Trusted, bounded lab coordinator. Disk preparation precedes this program.
// One TCP request carries device metadata, never RAM/CPU snapshot data.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/wait.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define CAP (64*1024)
#define PORT 19091
#define MAGIC 0x43585233u
struct request {
 uint32_t magic, state_len, manifest_len, first, operation, observe, persistent;
 uint64_t vm, epoch, canonical_start, canonical_bytes;
 char nonce[33], label[96], peer[16];
};
struct api_result { double us; int status; };
struct reply { uint32_t magic, ack_len; struct api_result receive; uint64_t ack_ns; };
struct outcome {
 struct api_result send;
 struct reply target;
 uint64_t handoff_ns, connection_ns, pause_to_resume_ns;
 uint32_t state_len, manifest_len;
};
static char directory[160], socket_path[192];
static int readiness_pipe = -1;
static int api_fd = -1;
static int peer_fd = -1;
static char peer_ip[16];
static void fail(const char *s) { perror(s); exit(1); }
static uint64_t ns(void) { struct timespec t; if(clock_gettime(CLOCK_MONOTONIC,&t))fail("clock");return (uint64_t)t.tv_sec*1000000000+t.tv_nsec; }
static void check(int ok,const char *s) { if(!ok){fprintf(stderr,"resident-control: %s\n",s);exit(1);} }
static void send_all(int fd,const void *p,size_t n) { while(n){ssize_t k=send(fd,p,n,MSG_NOSIGNAL);if(k<0&&errno==EINTR)continue;if(k<=0)fail("send");p=(const char *)p+k;n-=k;} }
static void recv_all(int fd,void *p,size_t n) { while(n){ssize_t k=read(fd,p,n);if(k<0&&errno==EINTR)continue;if(k<=0)fail("receive");p=(char *)p+k;n-=k;} }
static void write_all(int fd,const void *p,size_t n) { while(n){ssize_t k=write(fd,p,n);if(k<0&&errno==EINTR)continue;if(k<=0)fail("write");p=(const char *)p+k;n-=k;} }
static void timeout(int fd) { struct timeval t={.tv_sec=30};if(setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&t,sizeof(t))||setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&t,sizeof(t)))fail("timeout"); }
static void path(char *out,size_t cap,const char *name) { check(snprintf(out,cap,"%s/%s",directory,name)>0,"path"); }
static size_t read_file(const char *name,char *data) {
 char p[256];path(p,sizeof(p),name);int fd=open(p,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)fail("read file");
 struct stat s;if(fstat(fd,&s))fail("stat");check(S_ISREG(s.st_mode)&&s.st_size>0&&s.st_size<CAP,"metadata size");
 recv_all(fd,data,s.st_size);close(fd);return s.st_size;
}
static void write_file(const char *name,const char *data,size_t len) {
 char p[256],tmp[272];path(p,sizeof(p),name);check(snprintf(tmp,sizeof(tmp),"%s.tmp",p)>0,"temp path");
 int fd=open(tmp,O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0600);if(fd<0)fail("write file");write_all(fd,data,len);
 if(close(fd)||rename(tmp,p))fail("publish file");
}
static unsigned long long process_start(int pid) {
 char p[64],line[4096];snprintf(p,sizeof(p),"/proc/%d/stat",pid);FILE *f=fopen(p,"r");if(!f||!fgets(line,sizeof(line),f))fail("process stat");fclose(f);
 char *s=strrchr(line,')'),*save=NULL;check(s!=NULL,"process stat format");char *v=strtok_r(s+1," ",&save);
 for(int i=3;i<22&&v;i++) { v=strtok_r(NULL," ",&save); }
 check(v!=NULL,"process starttime");return strtoull(v,NULL,10);
}
static int owner_pid(void) {
 char p[256];path(p,sizeof(p),"fc.pid");FILE *f=fopen(p,"r");int pid=0;unsigned long long start=0;
 check(f&&fscanf(f,"%d %llu",&pid,&start)==2&&pid>1,"owner pid file");fclose(f);
 check(process_start(pid)==start,"owner PID reused");return pid;
}
static void select_owner(int pid) { char b[32];int n=snprintf(b,sizeof(b),"%d\n",pid);int fd=open("/sys/module/kvm/parameters/dax_owner_pid",O_WRONLY|O_CLOEXEC);if(fd<0)fail("owner parameter");write_all(fd,b,n);close(fd); }
static struct api_result api(const char *method,const char *route,const char *body) {
 struct api_result result={0};uint64_t start=ns();
 if(api_fd<0) {
 int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)fail("API socket");timeout(fd);
 struct sockaddr_un a={.sun_family=AF_UNIX};check(strlen(socket_path)<sizeof(a.sun_path),"API path");strcpy(a.sun_path,socket_path);
 if(connect(fd,(void*)&a,sizeof(a))) { fail("API connect"); }
 api_fd=fd;
 }
 int fd=api_fd;
 char q[2048];int len=snprintf(q,sizeof(q),"%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: %zu\r\n\r\n%s",method,route,strlen(body),body);
 check(len>0&&(size_t)len<sizeof(q),"API request bound");send_all(fd,q,len);size_t got=0;
 char response[1024];
 while(got<sizeof(response)-1){ssize_t n=read(fd,response+got,sizeof(response)-1-got);if(n<=0)fail("API response");got+=n;response[got]=0;if(strstr(response,"\r\n\r\n"))break;}
 result.us=(ns()-start)/1000.;check(sscanf(response,"HTTP/1.1 %d",&result.status)==1&&result.status==204,"API did not return 204");return result;
}
static size_t guest_ack(const char *label,char *out,off_t offset,uint64_t *elapsed) {
 check(strlen(label)<90&&strspn(label,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:.")==strlen(label),"ACK label");
 char p[256],command[128],needle[128];path(p,sizeof(p),"input");int input=open(p,O_WRONLY|O_NONBLOCK|O_CLOEXEC);if(input<0)fail("guest input");
 snprintf(command,sizeof(command),"ping %s\n",label);uint64_t start=ns();write_all(input,command,strlen(command));close(input);
 path(p,sizeof(p),"fc.log");int log=open(p,O_RDONLY|O_CLOEXEC);if(log<0)fail("guest log");snprintf(needle,sizeof(needle),"\"label\":\"%s\"",label);
 char buffer[8192];size_t used=0;
 while(ns()-start<30000000000ull){ssize_t n=pread(log,buffer+used,sizeof(buffer)-1-used,offset);if(n<0)fail("guest log read");offset+=n;used+=n;buffer[used]=0;
  char *line=buffer,*end;while((end=strchr(line,'\n'))){*end=0;if(strstr(line,"{\"event\":\"trace_ping\"")==line&&strstr(line,needle)){size_t size=strlen(line);if(size&&line[size-1]=='\r')line[--size]=0;check(size<4096,"ACK bound");memcpy(out,line,size+1);close(log);*elapsed=ns()-start;return size;}line=end+1;}
  size_t remaining=buffer+used-line;memmove(buffer,line,remaining);used=remaining;check(used<sizeof(buffer)-1,"guest serial line bound");struct timespec delay={.tv_nsec=50000};nanosleep(&delay,NULL);
 }
 check(0,"guest ACK timeout");return 0;
}
static int tcp(const char *ip,int listen_mode) {
 int fd=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)fail("TCP socket");
 // A parked VM may stay away for an arbitrarily long workload. Bound active
 // requests, but never expire the listener merely because no handoff arrives.
 if(!listen_mode)timeout(fd);
 int one=1;
 if(listen_mode && setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof(one)))fail("TCP reuse address");
 if(setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one))) { fail("TCP_NODELAY"); }
 struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(PORT)};
 check(inet_pton(AF_INET,ip,&a.sin_addr)==1,"IP address");if(listen_mode){if(bind(fd,(void*)&a,sizeof(a))||listen(fd,2))fail("TCP listen");}else if(connect(fd,(void*)&a,sizeof(a)))fail("TCP connect");return fd;
}
static int local_socket(int listen_mode) {
 int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)fail("local socket");
 struct sockaddr_un a={.sun_family=AF_UNIX};char p[256];path(p,sizeof(p),"control/request.sock");
 check(strlen(p)<sizeof(a.sun_path),"local request path");strcpy(a.sun_path,p);
 if(listen_mode){if(bind(fd,(void*)&a,sizeof(a))||listen(fd,2))fail("local listen");}
 else {timeout(fd);if(connect(fd,(void*)&a,sizeof(a)))fail("local connect");}
 return fd;
}
static void print_api(const char *name,const struct api_result *a) {
 printf("\"%s\":{\"status\":%d,\"duration_us\":%.3f}",name,a->status,a->us);
}


static void validate_request(const struct request *q,const char *nonce) {
 check(q->magic==MAGIC && q->nonce[32]==0 && !strcmp(q->nonce,nonce)
       && q->label[95]==0 && q->label[0] && q->first<=1 && q->operation<=1 && q->observe<=1 && q->persistent<=1,"request identity");
 check(strlen(q->label)<90 && strspn(q->label,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:.")==strlen(q->label),"request label");
 if(q->operation==1) {
  struct in_addr address;
  check(q->peer[15]==0 && inet_pton(AF_INET,q->peer,&address)==1 && q->epoch>0,"handoff peer/epoch");
 }
 if(q->epoch) check(q->canonical_start%4096==0 && q->canonical_bytes>0
                  && q->canonical_bytes%4096==0 && q->canonical_start<=UINT64_MAX-q->canonical_bytes,"plan bounds");
}
static struct outcome handoff(struct request q,const char *peer,int client,char *ack) {
 (void)owner_pid();struct outcome o={0};int fd;
 q.persistent=client>=0;
 if(q.persistent&&peer_fd>=0){check(!strcmp(peer,peer_ip),"cached peer changed");fd=peer_fd;}
 else {
  uint64_t connect_start=ns();fd=tcp(peer,0);o.connection_ns=ns()-connect_start;
  if(q.persistent){peer_fd=fd;strcpy(peer_ip,peer);}
 }
 uint64_t start=ns();o.send=api("PUT","/actions","{\"action_type\": \"DirectSend\"}");select_owner(2147483647);
 char wire[sizeof(q)+2*CAP],*state=wire+sizeof(q),*manifest; q.operation=0;
 q.state_len=read_file("direct-export.json",state);manifest=state+q.state_len;
 q.manifest_len=read_file("memory-manifest.json",manifest);
 o.state_len=q.state_len;o.manifest_len=q.manifest_len;
 memcpy(wire,&q,sizeof(q));send_all(fd,wire,sizeof(q)+q.state_len+q.manifest_len);
 uint32_t marker;recv_all(fd,&marker,sizeof(marker));check(marker==MAGIC,"resume marker");o.pause_to_resume_ns=ns()-start;
 if(client>=0)send_all(client,&marker,sizeof(marker));
 recv_all(fd,&o.target,sizeof(o.target));check(o.target.magic==MAGIC&&o.target.ack_len<4096,"reply bounds");
 recv_all(fd,ack,o.target.ack_len);ack[o.target.ack_len]=0;o.handoff_ns=ns()-start;if(!q.persistent)close(fd);return o;
}
static void print_outcome(const struct outcome *o,const char *ack,uint64_t request_ns,uint64_t request_resume_ns) {
 printf("RESIDENT_FAST_JSON {\"version\":2,\"wire_version\":3,\"handoff_ns\":%"PRIu64",\"connection_prepare_ns\":%"PRIu64
        ",\"guest_ack_ns\":%"PRIu64",\"pause_to_resume_response_ns\":%"PRIu64
        ",\"request_ns\":%"PRIu64",\"request_to_resume_response_ns\":%"PRIu64
        ",\"state_bytes\":%u,\"manifest_bytes\":%u,\"ack\":%s,\"apis\":{",
        o->handoff_ns,o->connection_ns,o->target.ack_ns,o->pause_to_resume_ns,request_ns,request_resume_ns,o->state_len,o->manifest_len,o->target.ack_len?ack:"null");
 print_api("send",&o->send);putchar(',');print_api("receive",&o->target.receive);puts("}}");
}
static uint64_t number(const char *s) {
 char *end;errno=0;unsigned long long n=strtoull(s,&end,10);
 check(s[0] && strspn(s,"0123456789")==strlen(s) && !errno && !*end,"integer argument");return n;
}
int main(int argc,char **argv) {
 check(argc==5||argc==7||argc==12||argc==13,"usage: serve DIR IP NONCE | handoff DIR PEER NONCE FIRST LABEL | request DIR LOCAL_IP NONCE FIRST LABEL PEER VM EPOCH START BYTES [OBSERVE]");
 check(strlen(argv[2])<sizeof(directory)&&!strncmp(argv[2],"/run/swe-pool-",14),"owned directory required");strcpy(directory,argv[2]);path(socket_path,sizeof(socket_path),"api.sock");
 check(strlen(argv[4])==32&&strspn(argv[4],"0123456789abcdef")==32,"session nonce");signal(SIGPIPE,SIG_IGN);

 if(!strcmp(argv[1],"launch")) {
  check(argc==5,"launch arguments");uint64_t begin=ns();char control[256],log[256];
  path(control,sizeof(control),"control");path(log,sizeof(log),"control/log");
  if(mkdir(control,0700))fail("fresh controller directory");
  int pipefd[2];if(pipe2(pipefd,O_CLOEXEC))fail("readiness pipe");uint64_t fork_start=ns();
  int pid=fork();if(pid<0)fail("controller fork");
  if(pid) {
   close(pipefd[1]);unsigned long long identity=process_start(pid);char record[80];
   int len=snprintf(record,sizeof(record),"%d %llu\n",pid,identity);write_file("control/fc.pid",record,len);
   struct pollfd f={.fd=pipefd[0],.events=POLLIN};int polled;
   do {polled=poll(&f,1,30000);}while(polled<0&&errno==EINTR);
   if(polled<=0){kill(pid,SIGKILL);waitpid(pid,NULL,0);check(0,"controller readiness timeout");}
   char ready;recv_all(pipefd[0],&ready,1);check(ready=='R',"controller readiness");close(pipefd[0]);uint64_t end=ns();
   printf("RESIDENT_SERVER_BOOT_JSON {\"pid\":%d,\"starttime\":%llu,\"fork_to_ready_ns\":%"PRIu64",\"native_ready_ns\":%"PRIu64"}\n",
          pid,identity,end-fork_start,end-begin);return 0;
  }
  close(pipefd[0]);readiness_pipe=pipefd[1];
  int in=open("/dev/null",O_RDONLY),out=open(log,O_WRONLY|O_CREAT|O_EXCL,0600);
  if(in<0||out<0||dup2(in,0)<0||dup2(out,1)<0||dup2(out,2)<0)_exit(121);
  close(in);close(out);argv[1]="serve";
 }
 if(!strcmp(argv[1],"serve")){
  check(argc==5,"serve arguments");int listener=tcp(argv[3],1),local=local_socket(1);puts("RESIDENT_CONTROL_READY");fflush(stdout);
  if(readiness_pipe>=0){write_all(readiness_pipe,"R",1);close(readiness_pipe);readiness_pipe=-1;}
  for(;;){
   struct pollfd watched[3]={{.fd=listener,.events=POLLIN},{.fd=local,.events=POLLIN},{.fd=peer_fd,.events=POLLIN}};
   int polled;do {polled=poll(watched,3,-1);}while(polled<0&&errno==EINTR);if(polled<0)fail("request poll");
   int trigger=!!watched[1].revents,fd;
   if(watched[2].revents) {check(!trigger,"concurrent ownership request");fd=peer_fd;}
   else {fd=accept4(trigger?local:listener,NULL,NULL,SOCK_CLOEXEC);if(fd<0)fail("accept");timeout(fd);
    if(!trigger){int one=1;if(setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one)))fail("accepted TCP_NODELAY");}}
   struct request q;recv_all(fd,&q,sizeof(q));validate_request(&q,argv[4]);
   check(q.operation==(unsigned)trigger,"wrong request channel");
   char ack[4096];
   if(q.operation==1) {
    check(q.state_len==0&&q.manifest_len==0,"trigger contains metadata");
    struct outcome o=handoff(q,q.peer,fd,ack);send_all(fd,&o,sizeof(o));send_all(fd,ack,o.target.ack_len);close(fd);continue;
   }
   if(q.persistent && peer_fd<0) {
    struct sockaddr_in address;socklen_t size=sizeof(address);
    if(getpeername(fd,(void*)&address,&size)||!inet_ntop(AF_INET,&address.sin_addr,peer_ip,sizeof(peer_ip)))fail("peer identity");
    peer_fd=fd;
   }
   check(!q.persistent||peer_fd==fd,"multiple peer connections");
   check(q.state_len>0&&q.state_len<CAP&&q.manifest_len>0&&q.manifest_len<CAP,"request bounds");
   char state[CAP],manifest[CAP];recv_all(fd,state,q.state_len);recv_all(fd,manifest,q.manifest_len);
   write_file("direct-import.json",state,q.state_len);write_file("restore-manifest.json",manifest,q.manifest_len);
   select_owner(owner_pid());
   struct reply r={.magic=MAGIC};off_t offset=0;
   if(q.observe){char p[256];path(p,sizeof(p),"fc.log");struct stat st;if(stat(p,&st))fail("log offset");offset=st.st_size;}
   r.receive=api("PUT","/actions","{\"action_type\": \"DirectReceive\"}");
   uint32_t marker=MAGIC;send_all(fd,&marker,sizeof(marker));
   if(q.observe)r.ack_len=guest_ack(q.label,ack,offset,&r.ack_ns);
   send_all(fd,&r,sizeof(r));send_all(fd,ack,r.ack_len);if(!q.persistent)close(fd);
  }
 }
 check((!strcmp(argv[1],"handoff")&&argc==7)||(!strcmp(argv[1],"request")&&(argc==12||argc==13)),"command arguments");
 check(!strcmp(argv[5],"0")||!strcmp(argv[5],"1"),"first import flag");
 struct request q={.magic=MAGIC,.first=(uint32_t)atoi(argv[5])};strcpy(q.nonce,argv[4]);check(strlen(argv[6])<sizeof(q.label),"label length");strcpy(q.label,argv[6]);
 char ack[4096];struct outcome o;
 if(argc>=12) {
  if(argc==13){check(!strcmp(argv[12],"1"),"observe argument");q.observe=1;}
  q.operation=1;check(strlen(argv[7])<sizeof(q.peer),"peer length");strcpy(q.peer,argv[7]);
  q.vm=number(argv[8]);q.epoch=number(argv[9]);q.canonical_start=number(argv[10]);q.canonical_bytes=number(argv[11]);validate_request(&q,argv[4]);
  uint64_t start=ns();int fd=local_socket(0);send_all(fd,&q,sizeof(q));
  uint32_t marker;recv_all(fd,&marker,sizeof(marker));check(marker==MAGIC,"request resume marker");uint64_t resumed=ns()-start;
  recv_all(fd,&o,sizeof(o));check(o.target.magic==MAGIC&&o.target.ack_len<sizeof(ack),"request reply bounds");
  recv_all(fd,ack,o.target.ack_len);ack[o.target.ack_len]=0;uint64_t elapsed=ns()-start;close(fd);
  print_outcome(&o,ack,elapsed,resumed);
 } else {
  validate_request(&q,argv[4]);o=handoff(q,argv[3],-1,ack);print_outcome(&o,ack,0,0);
 }
 return 0;
}
