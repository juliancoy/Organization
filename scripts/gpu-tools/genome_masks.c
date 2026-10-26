#include <vulkan/vulkan.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#define OK(x) do{VkResult r=(x);if(r!=VK_SUCCESS){fprintf(stderr,"%s: %d\n",#x,r);exit(2);}}while(0)
#define METRICS 69
static double ms(struct timespec a,struct timespec b){return (b.tv_sec-a.tv_sec)*1000.0+(b.tv_nsec-a.tv_nsec)/1e6;}
static void oracle(const uint32_t *data,size_t count,uint64_t *out){
 for(size_t i=0;i<count;i++){
  uint32_t states[6];
  for(int s=0;s<6;s++){
   uint32_t b=(data[i]>>(s*4))&15;int ref=b&1,span=b&2,start=b&8;
   if(span)out[18+s]++;
   uint32_t state=0;
   if(!(data[i]&0x80000000u)&&!(b&4)&&!(ref&&span)){
    if(span&&start)state=2;else if(ref&&!span&&!start)state=1;
   }
   states[s]=state;out[(state==1?0:state==2?6:12)+s]++;
  }
  int pair=0;for(int a=0;a<6;a++)for(int b=a+1;b<6;b++){
   if(states[a]&&states[b]){out[24+pair*3]++;out[25+pair*3+(states[a]==states[b]?0:1)]++;}pair++;
  }
 }
}
int main(int argc,char **argv){
 if(argc<3){fprintf(stderr,"usage: genome_masks INPUT.bin|- SHADER.spv [tile_positions]\n");return 6;}
 size_t tile=argc>3?strtoul(argv[3],0,10):1048576;
 if(tile<1||tile>1048576){fprintf(stderr,"Tile must contain1..1048576positions\n");return 6;}
 const char *input_path=!strcmp(argv[1],"--cpu-only")?argv[2]:argv[1];FILE *input=strcmp(input_path,"-")?fopen(input_path,"rb"):stdin;if(!input)return 6;
 uint32_t *data=calloc(tile,4);size_t threads=(tile+1023)/1024,inputwords=tile,bufferbytes=(inputwords+threads*METRICS)*4;
 uint64_t cpu[METRICS]={0},gpu[METRICS]={0};uint64_t positions=0,tiles=0;
 double cpu_ms=0,cpu_thread_ms=0,gpu_ms=0,setup_ms=0;struct timespec setup,ready;clock_gettime(CLOCK_MONOTONIC,&setup);
 if(!strcmp(argv[1],"--cpu-only")){
  struct timespec wall0,wall1,thread0,thread1;clock_gettime(CLOCK_MONOTONIC,&wall0);clock_gettime(CLOCK_THREAD_CPUTIME_ID,&thread0);
  while(1){size_t bytes=fread(data,1,tile*4,input);if(bytes%4)return 6;size_t count=bytes/4;if(!count)break;oracle(data,count,cpu);positions+=count;tiles++;}
  clock_gettime(CLOCK_THREAD_CPUTIME_ID,&thread1);clock_gettime(CLOCK_MONOTONIC,&wall1);if(ferror(input)||!positions)return 6;
  printf("{\"backend\":\"cpu_oracle\",\"positions\":%llu,\"tiles\":%llu,\"cpu_oracle_thread_ms\":%.3f,\"cpu_oracle_ms\":%.3f,\"counts\":[",(unsigned long long)positions,(unsigned long long)tiles,ms(thread0,thread1),ms(wall0,wall1));
  for(int j=0;j<METRICS;j++){printf("%s%llu",j?",":"",(unsigned long long)cpu[j]);}printf("]}\n");free(data);return 0;
 }
 VkInstance in; VkInstanceCreateInfo ici={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};OK(vkCreateInstance(&ici,0,&in));
 uint32_t n=0;OK(vkEnumeratePhysicalDevices(in,&n,0));VkPhysicalDevice *ps=calloc(n,sizeof(*ps));OK(vkEnumeratePhysicalDevices(in,&n,ps));VkPhysicalDevice p=VK_NULL_HANDLE;VkPhysicalDeviceProperties pp;
 for(uint32_t i=0;i<n;i++){vkGetPhysicalDeviceProperties(ps[i],&pp);if((pp.deviceType==VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU || pp.deviceType==VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU)){p=ps[i];break;}}
 if(!p){fprintf(stderr,"No physical GPU; software fallback rejected\n");return 3;}
 uint32_t qn=0;vkGetPhysicalDeviceQueueFamilyProperties(p,&qn,0);VkQueueFamilyProperties *qs=calloc(qn,sizeof(*qs));vkGetPhysicalDeviceQueueFamilyProperties(p,&qn,qs);uint32_t qi=0;while(qi<qn && !(qs[qi].queueFlags&VK_QUEUE_COMPUTE_BIT))qi++;
 if(qi==qn){fprintf(stderr,"No physical compute queue\n");return 3;}float priority=1;VkDeviceQueueCreateInfo qci={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=qi,.queueCount=1,.pQueuePriorities=&priority};VkDeviceCreateInfo dci={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&qci};VkDevice d;OK(vkCreateDevice(p,&dci,0,&d));VkQueue queue;vkGetDeviceQueue(d,qi,0,&queue);
 VkBuffer b;VkBufferCreateInfo bi={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=bufferbytes,.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,.sharingMode=VK_SHARING_MODE_EXCLUSIVE};OK(vkCreateBuffer(d,&bi,0,&b));VkMemoryRequirements mr;vkGetBufferMemoryRequirements(d,b,&mr);VkPhysicalDeviceMemoryProperties mp;vkGetPhysicalDeviceMemoryProperties(p,&mp);uint32_t mi=0;while(mi<mp.memoryTypeCount && (!(mr.memoryTypeBits&(1u<<mi)) || (mp.memoryTypes[mi].propertyFlags&(VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))!=(VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)))mi++;
 if(mi==mp.memoryTypeCount){return 4;}
 VkMemoryAllocateInfo ai={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.allocationSize=mr.size,.memoryTypeIndex=mi};VkDeviceMemory mem;OK(vkAllocateMemory(d,&ai,0,&mem));OK(vkBindBufferMemory(d,b,mem,0));uint32_t *mapped;OK(vkMapMemory(d,mem,0,VK_WHOLE_SIZE,0,(void**)&mapped));
 VkDescriptorSetLayoutBinding binding={.binding=0,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=1,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT};VkDescriptorSetLayoutCreateInfo li={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,.bindingCount=1,.pBindings=&binding};VkDescriptorSetLayout layout;OK(vkCreateDescriptorSetLayout(d,&li,0,&layout));VkPipelineLayoutCreateInfo pli={.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,.setLayoutCount=1,.pSetLayouts=&layout,.pushConstantRangeCount=1,.pPushConstantRanges=&(VkPushConstantRange){.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT,.offset=0,.size=8}};VkPipelineLayout pl;OK(vkCreatePipelineLayout(d,&pli,0,&pl));
 FILE*f=fopen(argv[2],"rb");if(!f){perror("shader");return 8;}fseek(f,0,SEEK_END);size_t sz=ftell(f);rewind(f);uint32_t*code=malloc(sz);if(fread(code,1,sz,f)!=sz)return 8;fclose(f);VkShaderModuleCreateInfo si={.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,.codeSize=sz,.pCode=code};VkShaderModule shader;OK(vkCreateShaderModule(d,&si,0,&shader));VkComputePipelineCreateInfo ci={.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,.stage={.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,.stage=VK_SHADER_STAGE_COMPUTE_BIT,.module=shader,.pName="main"},.layout=pl};VkPipeline pipeline;OK(vkCreateComputePipelines(d,VK_NULL_HANDLE,1,&ci,0,&pipeline));
 VkDescriptorPoolSize poolSize={.type=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=1};VkDescriptorPoolCreateInfo pci={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,.maxSets=1,.poolSizeCount=1,.pPoolSizes=&poolSize};VkDescriptorPool pool;OK(vkCreateDescriptorPool(d,&pci,0,&pool));VkDescriptorSetAllocateInfo dai={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,.descriptorPool=pool,.descriptorSetCount=1,.pSetLayouts=&layout};VkDescriptorSet set;OK(vkAllocateDescriptorSets(d,&dai,&set));VkDescriptorBufferInfo dbi={.buffer=b,.offset=0,.range=VK_WHOLE_SIZE};VkWriteDescriptorSet wr={.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=set,.dstBinding=0,.descriptorCount=1,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.pBufferInfo=&dbi};vkUpdateDescriptorSets(d,1,&wr,0,0);

 VkCommandPoolCreateInfo cpi={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,.queueFamilyIndex=qi};VkCommandPool cp;OK(vkCreateCommandPool(d,&cpi,0,&cp));
 VkCommandBufferAllocateInfo cai={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=cp,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};VkCommandBuffer cb;OK(vkAllocateCommandBuffers(d,&cai,&cb));
 VkFence fence;VkFenceCreateInfo fi={.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};OK(vkCreateFence(d,&fi,0,&fence));
 clock_gettime(CLOCK_MONOTONIC,&ready);setup_ms=ms(setup,ready);
 while(1){
  size_t bytes=fread(data,1,tile*4,input);if(bytes%4){fprintf(stderr,"Partial packed mask word\n");return 6;}size_t count=bytes/4;if(!count)break;
  struct timespec t0,t1,c0,c1;clock_gettime(CLOCK_MONOTONIC,&t0);clock_gettime(CLOCK_THREAD_CPUTIME_ID,&c0);uint64_t expected[METRICS]={0};oracle(data,count,expected);clock_gettime(CLOCK_THREAD_CPUTIME_ID,&c1);clock_gettime(CLOCK_MONOTONIC,&t1);cpu_ms+=ms(t0,t1);cpu_thread_ms+=ms(c0,c1);
  clock_gettime(CLOCK_MONOTONIC,&t0);memcpy(mapped,data,count*4);memset(mapped+inputwords,0,threads*METRICS*4);
  OK(vkResetCommandBuffer(cb,0));VkCommandBufferBeginInfo cbi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};OK(vkBeginCommandBuffer(cb,&cbi));
  vkCmdBindPipeline(cb,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);vkCmdBindDescriptorSets(cb,VK_PIPELINE_BIND_POINT_COMPUTE,pl,0,1,&set,0,0);
  uint32_t params[2]={(uint32_t)count,(uint32_t)inputwords};vkCmdPushConstants(cb,pl,VK_SHADER_STAGE_COMPUTE_BIT,0,8,params);size_t active=(count+1023)/1024;vkCmdDispatch(cb,(active+63)/64,1,1);
  VkMemoryBarrier mb={.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER,.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT,.dstAccessMask=VK_ACCESS_HOST_READ_BIT};vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&mb,0,0,0,0);OK(vkEndCommandBuffer(cb));
  OK(vkResetFences(d,1,&fence));VkSubmitInfo sub={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cb};OK(vkQueueSubmit(queue,1,&sub,fence));OK(vkWaitForFences(d,1,&fence,VK_TRUE,30000000000ull));
  uint64_t actual[METRICS]={0};for(size_t i=0;i<active;i++)for(int j=0;j<METRICS;j++)actual[j]+=mapped[inputwords+i*METRICS+j];clock_gettime(CLOCK_MONOTONIC,&t1);gpu_ms+=ms(t0,t1);
  for(int j=0;j<METRICS;j++){if(actual[j]!=expected[j]){fprintf(stderr,"GPU mask count mismatch tile%llu metric%d\n",(unsigned long long)tiles,j);return 9;}gpu[j]+=actual[j];cpu[j]+=expected[j];}
  positions+=count;tiles++;
 }
 if(ferror(input)||!positions){fprintf(stderr,"Empty or unreadable input\n");return 6;}
 printf("{\"device\":\"%s\",\"device_type\":%u,\"positions\":%llu,\"tiles\":%llu,\"tile_positions\":%zu,\"buffer_bytes\":%zu,\"cpu_oracle_ms\":%.3f,\"cpu_oracle_thread_ms\":%.3f,\"gpu_upload_dispatch_readback_ms\":%.3f,\"gpu_setup_ms\":%.3f,\"exact_match\":true,\"software_fallback\":false,\"counts\":[",pp.deviceName,pp.deviceType,(unsigned long long)positions,(unsigned long long)tiles,tile,bufferbytes,cpu_ms,cpu_thread_ms,gpu_ms,setup_ms);
 for(int j=0;j<METRICS;j++){printf("%s%llu",j?",":"",(unsigned long long)gpu[j]);}printf("]}\n");
 vkDeviceWaitIdle(d);vkDestroyDevice(d,0);vkDestroyInstance(in,0);free(data);return 0;
}
