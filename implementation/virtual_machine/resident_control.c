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
#include <sys/syscall.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include "resident_wire.h"

#define CAP (64*1024)
#define PORT 19091
#define MAGIC RESIDENT_MAGIC
static char directory[160], socket_path[192];
static int readiness_pipe = -1;
static int api_fd = -1;
static int native_fd = -1, native_enabled;
static int peer_fd = -1;
static int owner_fd = -1, prepared_owner_pid, selector_fd = -1;
struct metadata_file { int fd; size_t length; };
static struct metadata_file import_file={.fd=-1}, manifest_file={.fd=-1};
static char peer_ip[16];
static int profile_enabled;
static void fail(const char *s) { perror(s); exit(1); }
static uint64_t ns(void) { struct timespec t; if(clock_gettime(CLOCK_MONOTONIC,&t))fail("clock");return (uint64_t)t.tv_sec*1000000000+t.tv_nsec; }
static void check(int ok,const char *s) { if(!ok){fprintf(stderr,"resident-control: %s\n",s);exit(1);} }
struct phase_profile { uint64_t last; unsigned count; struct { const char *name; uint64_t ns; } stages[10]; };
static void phase_start(struct phase_profile *p) { memset(p,0,sizeof(*p));if(profile_enabled)p->last=ns(); }
static void phase_mark(struct phase_profile *p,const char *name) {
 if(!profile_enabled)return;
 check(p->count<10,"profile stage bound");uint64_t now=ns();
 p->stages[p->count].name=name;p->stages[p->count++].ns=now-p->last;p->last=now;
}
static void phase_print(const struct phase_profile *p,const struct request *q,const char *side) {
 if(!profile_enabled)return;
 fprintf(stderr,"RESIDENT_PHASES_JSON {\"label\":\"%s\",\"epoch\":%"PRIu64",\"side\":\"%s\",\"stages_ns\":{",q->label,q->epoch,side);
 for(unsigned i=0;i<p->count;i++)fprintf(stderr,"%s\"%s\":%"PRIu64,i?",":"",p->stages[i].name,p->stages[i].ns);
 fputs("}}\n",stderr);
}
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
static struct metadata_file prepare_metadata(const char *name) {
 char p[256];path(p,sizeof(p),name);
 int fd=open(p,O_WRONLY|O_CREAT|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK,0600);if(fd<0)fail("prepare metadata");
 struct stat st;if(fstat(fd,&st))fail("metadata stat");
 check(S_ISREG(st.st_mode)&&st.st_nlink==1&&st.st_size>=0&&st.st_size<CAP,"private metadata file");
 return (struct metadata_file){.fd=fd,.length=(size_t)st.st_size};
}
static void overwrite_metadata(struct metadata_file *file,const char *data,size_t len) {
 check(file->fd>=0&&len>0&&len<CAP,"metadata write bounds");
 size_t done=0;
 while(done<len) {
  ssize_t n=pwrite(file->fd,data+done,len-done,done);
  if(n<0&&errno==EINTR)continue;
  if(n<=0)fail("metadata write");
  done+=n;
 }
 if(len<file->length&&ftruncate(file->fd,len))fail("metadata shrink");
 file->length=len;
}
static unsigned long long process_start(int pid) {
 char p[64],line[4096];snprintf(p,sizeof(p),"/proc/%d/stat",pid);FILE *f=fopen(p,"r");if(!f||!fgets(line,sizeof(line),f))fail("process stat");fclose(f);
 char *s=strrchr(line,')'),*save=NULL;check(s!=NULL,"process stat format");char *v=strtok_r(s+1," ",&save);
 for(int i=3;i<22&&v;i++) { v=strtok_r(NULL," ",&save); }
 check(v!=NULL,"process starttime");return strtoull(v,NULL,10);
}
static int read_owner_pid(void) {
 char p[256];path(p,sizeof(p),"fc.pid");FILE *f=fopen(p,"r");int pid=0;unsigned long long start=0;
 check(f&&fscanf(f,"%d %llu",&pid,&start)==2&&pid>1,"owner pid file");fclose(f);
 check(process_start(pid)==start,"owner PID reused");return pid;
}
static int owner_pid(void) {
 if(owner_fd<0)return read_owner_pid();
 struct pollfd owner={.fd=owner_fd,.events=POLLIN};int result;
 do {result=poll(&owner,1,0);}while(result<0&&errno==EINTR);
 check(result==0,"prepared owner exited");return prepared_owner_pid;
}
static void prepare_owner(void) {
 int pid=read_owner_pid();
 int fd=syscall(SYS_pidfd_open,pid,0);if(fd<0)fail("owner pidfd");
 // Pin the process before trusting its cached PID. Recheck the original
 // PID/starttime record and reject an exit during this preparation window.
 check(read_owner_pid()==pid,"owner changed during preparation");
 prepared_owner_pid=pid;owner_fd=fd;(void)owner_pid();
}
static void select_owner(int pid) {
 char b[32];int n=snprintf(b,sizeof(b),"%d\n",pid);
 if(selector_fd>=0) {
  ssize_t written;do {written=pwrite(selector_fd,b,n,0);}while(written<0&&errno==EINTR);
  check(written==n,"prepared owner parameter write");return;
 }
 int fd=open("/sys/module/kvm/parameters/dax_owner_pid",O_WRONLY|O_CLOEXEC);if(fd<0)fail("owner parameter");
 write_all(fd,b,n);close(fd);
}
static void connect_api(void) {
 if(api_fd<0) {
 int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)fail("API socket");timeout(fd);
 struct sockaddr_un a={.sun_family=AF_UNIX};check(strlen(socket_path)<sizeof(a.sun_path),"API path");strcpy(a.sun_path,socket_path);
 if(connect(fd,(void*)&a,sizeof(a))) { fail("API connect"); }
 if(owner_fd>=0) {
  struct ucred peer;socklen_t size=sizeof(peer);
  if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&size))fail("API peer identity");
  check(peer.pid==owner_pid(),"API belongs to another process");
 }
 api_fd=fd;
 }
}
static int connect_native(void) {
 if(!native_enabled)return 0;
 if(native_fd>=0)return 1;
 int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)fail("native socket");timeout(fd);
 struct sockaddr_un address={.sun_family=AF_UNIX};
 check(snprintf(address.sun_path,sizeof(address.sun_path),"%s/resident.sock",directory)>0,"native path");
 if(connect(fd,(void*)&address,sizeof(address))) {
  int error=errno;close(fd);
  // Before the first attach the destination has only its preboot HTTP API.
  check(error==ENOENT||error==ECONNREFUSED,"native connect");return 0;
 }
 struct ucred peer;socklen_t size=sizeof(peer);
 if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&size))fail("native peer identity");
 check(peer.pid==owner_pid(),"native endpoint belongs to another process");
 native_fd=fd;
 unsigned char command=0;uint64_t response[2];
 send_all(fd,&command,1);recv_all(fd,response,sizeof(response));check(response[0]==0,"native ready");
 return 1;
}
static struct api_result api(const char *method,const char *route,const char *body) {
 if(connect_native()) {
  unsigned char command=!strcmp(body,"{\"action_type\": \"DirectSend\"}")?1:2;
  check(command==1||!strcmp(body,"{\"action_type\": \"DirectReceive\"}"),"native action");
  uint64_t start=ns(),response[2];send_all(native_fd,&command,1);recv_all(native_fd,response,sizeof(response));
  check(response[0]==0,"native handoff failed");
  return (struct api_result){.status=204,.us=(ns()-start)/1000.};
 }
 struct api_result result={0};uint64_t start=ns();connect_api();
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
       && q->label[95]==0 && q->label[0] && q->first<=1 && q->operation<=2 && q->observe<=1 && q->persistent<=1,"request identity");
 check(strlen(q->label)<90 && strspn(q->label,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:.")==strlen(q->label),"request label");
 if(q->operation==1 || q->operation==2) {
  struct in_addr address;
  check(q->peer[15]==0 && inet_pton(AF_INET,q->peer,&address)==1 && (q->operation==2 || q->epoch>0),"handoff peer/epoch");
 }
 if(q->epoch) check(q->canonical_start%4096==0 && q->canonical_bytes>0
                  && q->canonical_bytes%4096==0 && q->canonical_start<=UINT64_MAX-q->canonical_bytes,"plan bounds");
}
static struct outcome handoff(struct request q,const char *peer,int client,char *ack) {
 struct phase_profile profile;phase_start(&profile);
 (void)owner_pid();phase_mark(&profile,"owner_check");struct outcome o={0};int fd;
 q.persistent=client>=0;
 if(q.persistent&&peer_fd>=0){check(!strcmp(peer,peer_ip),"cached peer changed");fd=peer_fd;}
 else {
  uint64_t connect_start=ns();fd=tcp(peer,0);o.connection_ns=ns()-connect_start;
  if(q.persistent){peer_fd=fd;strcpy(peer_ip,peer);}
 }
 phase_mark(&profile,"peer_connection");
 uint64_t start=ns();o.send=api("PUT","/actions","{\"action_type\": \"DirectSend\"}");phase_mark(&profile,"export_api");
 select_owner(2147483647);phase_mark(&profile,"source_fence");
 char wire[sizeof(q)+2*CAP],*state=wire+sizeof(q),*manifest; q.operation=0;
 q.state_len=read_file("direct-export.json",state);manifest=state+q.state_len;
 q.manifest_len=read_file("memory-manifest.json",manifest);
 o.state_len=q.state_len;o.manifest_len=q.manifest_len;
 phase_mark(&profile,"metadata_read");
 memcpy(wire,&q,sizeof(q));send_all(fd,wire,sizeof(q)+q.state_len+q.manifest_len);
 phase_mark(&profile,"metadata_send");
 uint32_t marker;recv_all(fd,&marker,sizeof(marker));check(marker==MAGIC,"resume marker");o.pause_to_resume_ns=ns()-start;
 phase_mark(&profile,"peer_until_resume");
 if(client>=0)send_all(client,&marker,sizeof(marker));
 recv_all(fd,&o.target,sizeof(o.target));check(o.target.magic==MAGIC&&o.target.ack_len<4096,"reply bounds");
 recv_all(fd,ack,o.target.ack_len);ack[o.target.ack_len]=0;o.handoff_ns=ns()-start;if(!q.persistent)close(fd);
 phase_print(&profile,&q,"send");return o;
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
 const char *native_env=getenv("FC_RESIDENT_NATIVE");native_enabled=native_env&&!strcmp(native_env,"1");
 const char *profile_env=getenv("FC_RESIDENT_PROFILE");profile_enabled=profile_env&&!strcmp(profile_env,"1");
 check(argc==5||argc==6||argc==7||argc==12||argc==13,"usage: serve DIR IP NONCE | handoff DIR PEER NONCE FIRST LABEL | request DIR LOCAL_IP NONCE FIRST LABEL PEER VM EPOCH START BYTES [OBSERVE]");
 check(strlen(argv[2])<sizeof(directory)&&!strncmp(argv[2],"/run/swe-pool-",14),"owned directory required");strcpy(directory,argv[2]);path(socket_path,sizeof(socket_path),"api.sock");
 check(strlen(argv[4])==32&&strspn(argv[4],"0123456789abcdef")==32,"session nonce");signal(SIGPIPE,SIG_IGN);

 if(!strcmp(argv[1],"launch")) {
  check(argc==5||argc==6,"launch arguments");uint64_t begin=ns();char control[256],log[256];
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
  check(argc==5||argc==6,"serve arguments");int listener=tcp(argv[3],1),local=local_socket(1);
  prepare_owner();
  selector_fd=open("/sys/module/kvm/parameters/dax_owner_pid",O_WRONLY|O_CLOEXEC);
  if(selector_fd<0)fail("prepare owner parameter");
  connect_api();
  (void)connect_native();
  import_file=prepare_metadata("direct-import.json");
  manifest_file=prepare_metadata("restore-manifest.json");
  if(argc==6) {
   // Destination launch follows source readiness. Warm TCP/ARP while the VM
   // still runs on its source; this message never selects an execution owner.
   uint64_t begin=ns();struct request q={.magic=MAGIC,.operation=2,.persistent=1};
   check(strlen(argv[5])<sizeof(q.peer),"prepare peer length");strcpy(q.peer,argv[5]);strcpy(q.nonce,argv[4]);strcpy(q.label,"prepare");
   validate_request(&q,argv[4]);peer_fd=tcp(q.peer,0);strcpy(peer_ip,q.peer);send_all(peer_fd,&q,sizeof(q));
   uint32_t marker;recv_all(peer_fd,&marker,sizeof(marker));check(marker==MAGIC,"peer preparation acknowledgement");
   printf("RESIDENT_PEER_READY_JSON {\"connection_prepare_ns\":%"PRIu64",\"owner_changed\":false}\n",ns()-begin);
  }
  puts("RESIDENT_CONTROL_READY");fflush(stdout);
  if(readiness_pipe>=0){write_all(readiness_pipe,"R",1);close(readiness_pipe);readiness_pipe=-1;}
  for(;;){
   struct pollfd watched[3]={{.fd=listener,.events=POLLIN},{.fd=local,.events=POLLIN},{.fd=peer_fd,.events=POLLIN}};
   int polled;do {polled=poll(watched,3,-1);}while(polled<0&&errno==EINTR);if(polled<0)fail("request poll");
   int trigger=!!watched[1].revents,fd;
   if(watched[2].revents) {check(!trigger,"concurrent ownership request");fd=peer_fd;}
   else {fd=accept4(trigger?local:listener,NULL,NULL,SOCK_CLOEXEC);if(fd<0)fail("accept");timeout(fd);
    if(!trigger){int one=1;if(setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one)))fail("accepted TCP_NODELAY");}}
   struct request q;recv_all(fd,&q,sizeof(q));validate_request(&q,argv[4]);
   if(q.operation==2) {
    check(!trigger && peer_fd<0 && q.persistent && !q.state_len && !q.manifest_len && !q.epoch && !q.observe,
          "invalid peer preparation");
    struct sockaddr_in address;socklen_t size=sizeof(address);
    if(getpeername(fd,(void*)&address,&size)||!inet_ntop(AF_INET,&address.sin_addr,peer_ip,sizeof(peer_ip)))fail("prepared peer identity");
    peer_fd=fd;uint32_t marker=MAGIC;send_all(fd,&marker,sizeof(marker));continue;
   }
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
   struct phase_profile profile;phase_start(&profile);
   char state[CAP],manifest[CAP];recv_all(fd,state,q.state_len);recv_all(fd,manifest,q.manifest_len);
   phase_mark(&profile,"metadata_receive");
   // The parked VMM reads these private staging files only in DirectReceive.
   // This serialized controller is their sole writer. Complete both writes
   // before selecting the owner or sending that API; any error exits here.
   // Thus the API publishes the pair, without per-handoff inode replacement.
   overwrite_metadata(&import_file,state,q.state_len);overwrite_metadata(&manifest_file,manifest,q.manifest_len);
   phase_mark(&profile,"metadata_publish");
   int owner=owner_pid();phase_mark(&profile,"owner_check");select_owner(owner);phase_mark(&profile,"owner_select");
   struct reply r={.magic=MAGIC};off_t offset=0;
   if(q.observe){char p[256];path(p,sizeof(p),"fc.log");struct stat st;if(stat(p,&st))fail("log offset");offset=st.st_size;}
   r.receive=api("PUT","/actions","{\"action_type\": \"DirectReceive\"}");
   phase_mark(&profile,"import_api");
   uint32_t marker=MAGIC;send_all(fd,&marker,sizeof(marker));
   if(q.observe)r.ack_len=guest_ack(q.label,ack,offset,&r.ack_ns);
   send_all(fd,&r,sizeof(r));send_all(fd,ack,r.ack_len);if(!q.persistent)close(fd);
   phase_print(&profile,&q,"receive");
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
