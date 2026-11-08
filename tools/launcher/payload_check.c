/* 测试用：用与启动器相同的 miniz 读出单文件 exe 的负载，逐个解压校验。gcc -Iminiz payload_check.c miniz/miniz.c */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include "miniz.h"
int main(int c,char**v){FILE*f=fopen(v[1],"rb");fseek(f,0,SEEK_END);long n=ftell(f);fseek(f,0,SEEK_SET);
unsigned char*d=malloc(n);fread(d,1,n,f);fclose(f);
unsigned char*t=d+n-64; if(memcmp(t,"SWPKG01\0",8)){puts("bad magic");return 1;}
uint64_t off,sz;memcpy(&off,t+8,8);memcpy(&sz,t+16,8);
mz_zip_archive z;memset(&z,0,sizeof z);if(!mz_zip_reader_init_mem(&z,d+off,sz,0)){puts("init fail");return 2;}
unsigned cnt=mz_zip_reader_get_num_files(&z),bad=0;unsigned long long tot=0;
for(unsigned i=0;i<cnt;i++){size_t s;void*p=mz_zip_reader_extract_to_heap(&z,i,&s,0);if(!p&&!mz_zip_reader_is_file_a_directory(&z,i))bad++;tot+=s;free(p);}
printf("tag=%s files=%u bad=%u bytes=%llu\n",(char*)t+24,cnt,bad,tot);return bad!=0;}
