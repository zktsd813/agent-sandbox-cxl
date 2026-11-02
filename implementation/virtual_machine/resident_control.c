// SPDX-License-Identifier: Apache-2.0
// Trusted, bounded lab coordinator. Disk preparation precedes this program.
// One TCP request carries device metadata, never RAM/CPU snapshot data.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <netinet/tcp.h>
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
#define MAGIC 0x43585231u
struct request { uint32_t magic, state_len, manifest_len, first; char nonce[33], label[96]; };
struct api_result { double us; int status; char response[1024]; };
struct reply { uint32_t magic, ack_len; struct api_result attach, checkpoint, resume; uint64_t ack_ns; };
static char directory[160], socket_path[192];
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
 (void)owner_pid();struct api_result result={0};uint64_t start=ns();int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);if(fd<0)fail("API socket");timeout(fd);
 struct sockaddr_un a={.sun_family=AF_UNIX};check(strlen(socket_path)<sizeof(a.sun_path),"API path");strcpy(a.sun_path,socket_path);
 if(connect(fd,(void*)&a,sizeof(a))) { fail("API connect"); }
 char q[2048];int len=snprintf(q,sizeof(q),"%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: %zu\r\n\r\n%s",method,route,strlen(body),body);
 check(len>0&&(size_t)len<sizeof(q),"API request bound");send_all(fd,q,len);size_t got=0;
 while(got<sizeof(result.response)-1){ssize_t n=read(fd,result.response+got,sizeof(result.response)-1-got);if(n<=0)fail("API response");got+=n;result.response[got]=0;if(strstr(result.response,"\r\n\r\n"))break;}
 close(fd);result.us=(ns()-start)/1000.;check(sscanf(result.response,"HTTP/1.1 %d",&result.status)==1&&result.status==204,"API did not return 204");return result;
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
 if(setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one))) { fail("TCP_NODELAY"); }
 struct sockaddr_in a={.sin_family=AF_INET,.sin_port=htons(PORT)};
 check(inet_pton(AF_INET,ip,&a.sin_addr)==1,"IP address");if(listen_mode){if(bind(fd,(void*)&a,sizeof(a))||listen(fd,2))fail("TCP listen");}else if(connect(fd,(void*)&a,sizeof(a)))fail("TCP connect");return fd;
}
static void json_string(const char *p) { putchar('"');for(;*p;p++){unsigned char c=*p;if(c=='"'||c=='\\')printf("\\%c",c);else if(c<32)printf("\\u%04x",c);else putchar(c);}putchar('"'); }
static void print_api(const char *name,const struct api_result *a) { printf("\"%s\":{\"status\":%d,\"duration_us\":%.3f,\"response\":",name,a->status,a->us);json_string(a->response);putchar('}'); }
int main(int argc,char **argv) {
 check(argc==5||argc==7,"usage: resident-control serve DIRECTORY IP NONCE | handoff DIRECTORY TARGET_IP NONCE FIRST LABEL");
 check(strlen(argv[2])<sizeof(directory)&&!strncmp(argv[2],"/run/swe-pool-",14),"owned directory required");strcpy(directory,argv[2]);path(socket_path,sizeof(socket_path),"api.sock");
 check(strlen(argv[4])==32&&strspn(argv[4],"0123456789abcdef")==32,"session nonce");signal(SIGPIPE,SIG_IGN);
 if(!strcmp(argv[1],"serve")){
  check(argc==5,"serve arguments");int listener=tcp(argv[3],1);puts("RESIDENT_CONTROL_READY");fflush(stdout);
  for(;;){int fd=accept4(listener,NULL,NULL,SOCK_CLOEXEC);if(fd<0){if(errno==EINTR)continue;fail("accept");}timeout(fd);int one=1;setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&one,sizeof(one));
   struct request q;recv_all(fd,&q,sizeof(q));check(q.magic==MAGIC&&q.nonce[32]==0&&!strcmp(q.nonce,argv[4])&&q.label[95]==0,"request identity");check(strspn(q.label,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:.")==strlen(q.label),"request label");check(q.state_len>0&&q.state_len<CAP&&q.manifest_len>0&&q.manifest_len<CAP&&q.first<=1,"request bounds");
   char state[CAP],manifest[CAP],ack[4096];recv_all(fd,state,q.state_len);recv_all(fd,manifest,q.manifest_len);
   write_file("direct-import.json",state,q.state_len);write_file("restore-manifest.json",manifest,q.manifest_len);select_owner(owner_pid());
   struct reply r={.magic=MAGIC};r.attach=api("PUT","/actions",q.first?"{\"action_type\": \"InstanceStart\"}":"{\"action_type\": \"DirectImport\"}");
   r.checkpoint=api("PUT","/actions","{\"action_type\": \"PoolCache\"}");
   char p[256];path(p,sizeof(p),"fc.log");struct stat st;if(stat(p,&st))fail("log offset");r.resume=api("PATCH","/vm","{\"state\": \"Resumed\"}");
   r.ack_len=guest_ack(q.label,ack,st.st_size,&r.ack_ns);send_all(fd,&r,sizeof(r));send_all(fd,ack,r.ack_len);close(fd);
  }
 }
 check(!strcmp(argv[1],"handoff")&&argc==7&&(!strcmp(argv[5],"0")||!strcmp(argv[5],"1")),"handoff arguments");
 uint64_t prepare=ns();int fd=tcp(argv[3],0);prepare=ns()-prepare;
 struct request q={.magic=MAGIC,.first=(uint32_t)atoi(argv[5])};strcpy(q.nonce,argv[4]);check(strlen(argv[6])<sizeof(q.label),"label length");strcpy(q.label,argv[6]);check(strspn(q.label,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:.")==strlen(q.label),"label characters");
 uint64_t start=ns();struct api_result pause=api("PATCH","/vm","{\"state\": \"Paused\"}");struct api_result export=api("PUT","/actions","{\"action_type\": \"DirectExport\"}");select_owner(2147483647);
 char state[CAP],manifest[CAP],ack[4096];q.state_len=read_file("direct-export.json",state);q.manifest_len=read_file("memory-manifest.json",manifest);
 send_all(fd,&q,sizeof(q));send_all(fd,state,q.state_len);send_all(fd,manifest,q.manifest_len);
 struct reply r;recv_all(fd,&r,sizeof(r));check(r.magic==MAGIC&&r.ack_len>0&&r.ack_len<sizeof(ack),"reply bounds");recv_all(fd,ack,r.ack_len);ack[r.ack_len]=0;uint64_t end=ns();close(fd);
 printf("RESIDENT_FAST_JSON {\"version\":1,\"handoff_ns\":%"PRIu64",\"connection_prepare_ns\":%"PRIu64",\"guest_ack_ns\":%"PRIu64",\"state_bytes\":%u,\"manifest_bytes\":%u,\"ack\":%s,\"apis\":{",end-start,prepare,r.ack_ns,q.state_len,q.manifest_len,ack);
 print_api("pause",&pause);putchar(',');print_api("export",&export);putchar(',');print_api("attach",&r.attach);putchar(',');print_api("checkpoint",&r.checkpoint);putchar(',');print_api("resume",&r.resume);puts("}}");return 0;
}
