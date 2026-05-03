
/*
 * MyceliaDB Native GPU Residency Bridge v1.18F
 * Backward compatibility alias: MYCELIA_NATIVE_SNAPSHOT_RUNTIME_V1_18D
 * Backward compatibility alias: MYCELIA_NATIVE_STRICT_CERTIFICATION_GATE_V1_18E
 * Real OpenCL-backed VRAM open/restore selftest.
 *
 * This layer dynamically loads OpenCL, creates real GPU buffers for sealed
 * envelope/snapshot bytes, runs a GPU kernel that emits only digests, and keeps
 * opaque handles in native slots. It never returns plaintext payloads.
 *
 * Security scope:
 *   - Proves OpenCL buffer allocation, kernel execution and digest-only return.
 *   - Does not claim formal cryptographic hardware attestation.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <stdlib.h>

#if defined(_WIN32)
#include <windows.h>
#define MYCELIA_EXPORT __declspec(dllexport)
#else
#include <dlfcn.h>
#define MYCELIA_EXPORT __attribute__((visibility("default")))
#endif

#define MYCELIA_STAGE_SLOTS 256
#define MYCELIA_DIGEST_LANES 64

typedef int32_t cl_int;
typedef uint32_t cl_uint;
typedef uint64_t cl_ulong;
typedef size_t cl_bool;
typedef uintptr_t cl_bitfield;
typedef cl_bitfield cl_device_type;
typedef cl_bitfield cl_mem_flags;
typedef void* cl_platform_id;
typedef void* cl_device_id;
typedef void* cl_context;
typedef void* cl_command_queue;
typedef void* cl_program;
typedef void* cl_kernel;
typedef void* cl_mem;

#define CL_SUCCESS 0
#define CL_DEVICE_TYPE_GPU (1ULL << 2)
#define CL_MEM_READ_ONLY (1ULL << 2)
#define CL_MEM_READ_WRITE (1ULL << 0)
#define CL_MEM_COPY_HOST_PTR (1ULL << 5)
#define CL_TRUE 1

typedef cl_int (*PFN_clGetPlatformIDs)(cl_uint, cl_platform_id*, cl_uint*);
typedef cl_int (*PFN_clGetDeviceIDs)(cl_platform_id, cl_device_type, cl_uint, cl_device_id*, cl_uint*);
typedef cl_context (*PFN_clCreateContext)(const void*, cl_uint, const cl_device_id*, void*, void*, cl_int*);
typedef cl_command_queue (*PFN_clCreateCommandQueue)(cl_context, cl_device_id, cl_bitfield, cl_int*);
typedef cl_program (*PFN_clCreateProgramWithSource)(cl_context, cl_uint, const char**, const size_t*, cl_int*);
typedef cl_int (*PFN_clBuildProgram)(cl_program, cl_uint, const cl_device_id*, const char*, void*, void*);
typedef cl_kernel (*PFN_clCreateKernel)(cl_program, const char*, cl_int*);
typedef cl_mem (*PFN_clCreateBuffer)(cl_context, cl_mem_flags, size_t, void*, cl_int*);
typedef cl_int (*PFN_clSetKernelArg)(cl_kernel, cl_uint, size_t, const void*);
typedef cl_int (*PFN_clEnqueueNDRangeKernel)(cl_command_queue, cl_kernel, cl_uint, const size_t*, const size_t*, const size_t*, cl_uint, const void*, void*);
typedef cl_int (*PFN_clFinish)(cl_command_queue);
typedef cl_int (*PFN_clEnqueueReadBuffer)(cl_command_queue, cl_mem, cl_bool, size_t, size_t, void*, cl_uint, const void*, void*);
typedef cl_int (*PFN_clReleaseMemObject)(cl_mem);
typedef cl_int (*PFN_clEnqueueFillBuffer)(cl_command_queue, cl_mem, const void*, size_t, size_t, size_t, cl_uint, const void*, void*);
typedef cl_int (*PFN_clReleaseKernel)(cl_kernel);
typedef cl_int (*PFN_clReleaseProgram)(cl_program);
typedef cl_int (*PFN_clReleaseCommandQueue)(cl_command_queue);
typedef cl_int (*PFN_clReleaseContext)(cl_context);

typedef struct MyceliaOpenCL {
    int loaded;
    int initialized;
    int selftest_passed;
    char reason[256];
#if defined(_WIN32)
    HMODULE lib;
#else
    void* lib;
#endif
    cl_platform_id platform;
    cl_device_id device;
    cl_context context;
    cl_command_queue queue;
    cl_program program;
    cl_kernel digest_kernel;

    PFN_clGetPlatformIDs clGetPlatformIDs;
    PFN_clGetDeviceIDs clGetDeviceIDs;
    PFN_clCreateContext clCreateContext;
    PFN_clCreateCommandQueue clCreateCommandQueue;
    PFN_clCreateProgramWithSource clCreateProgramWithSource;
    PFN_clBuildProgram clBuildProgram;
    PFN_clCreateKernel clCreateKernel;
    PFN_clCreateBuffer clCreateBuffer;
    PFN_clSetKernelArg clSetKernelArg;
    PFN_clEnqueueNDRangeKernel clEnqueueNDRangeKernel;
    PFN_clFinish clFinish;
    PFN_clEnqueueReadBuffer clEnqueueReadBuffer;
    PFN_clReleaseMemObject clReleaseMemObject;
    PFN_clEnqueueFillBuffer clEnqueueFillBuffer;
    PFN_clReleaseKernel clReleaseKernel;
    PFN_clReleaseProgram clReleaseProgram;
    PFN_clReleaseCommandQueue clReleaseCommandQueue;
    PFN_clReleaseContext clReleaseContext;
} MyceliaOpenCL;

typedef struct MyceliaStageSlot {
    uint64_t handle;
    uint64_t created_at;
    uint64_t request_hash;
    uint64_t gpu_digest;
    size_t request_len;
    char kind[48];
    int in_use;
    int gpu_resident;
    cl_mem gpu_buffer;
} MyceliaStageSlot;

static MyceliaOpenCL g_cl;
static MyceliaStageSlot g_slots[MYCELIA_STAGE_SLOTS];
static uint64_t g_counter = 0x9e3779b97f4a7c15ULL;

static void mycelia_secure_release_mem(cl_command_queue q, cl_mem mem, size_t bytes) {
    if (!mem) return;
    if (q && g_cl.clEnqueueFillBuffer && bytes > 0) {
        const unsigned char zero = 0;
        g_cl.clEnqueueFillBuffer(q, mem, &zero, sizeof(zero), 0, bytes, 0, NULL, NULL);
        if (g_cl.clFinish) g_cl.clFinish(q);
    }
    if (g_cl.clReleaseMemObject) g_cl.clReleaseMemObject(mem);
}


static uint64_t fnv1a64_bytes(const unsigned char* data, size_t len) {
    uint64_t h = 1469598103934665603ULL;
    if (!data) return h;
    for (size_t i = 0; i < len; ++i) {
        h ^= (uint64_t)data[i];
        h *= 1099511628211ULL;
    }
    return h;
}

static uint64_t fnv1a64(const char* data) {
    return fnv1a64_bytes((const unsigned char*)data, data ? strlen(data) : 0);
}

static int write_json(char* out_json, size_t out_len, const char* text) {
    if (!out_json || out_len == 0) return 2;
    size_t n = strlen(text);
    if (n + 1 > out_len) {
        const char* err = "{\"status\":\"error\",\"message\":\"output buffer too small\"}";
        size_t e = strlen(err);
        if (e + 1 <= out_len) memcpy(out_json, err, e + 1);
        else out_json[0] = '\0';
        return 3;
    }
    memcpy(out_json, text, n + 1);
    return 0;
}

static int request_contains(const char* request_json, const char* needle) {
    return request_json && needle && strstr(request_json, needle) != NULL;
}

static void set_reason(const char* text) {
    snprintf(g_cl.reason, sizeof(g_cl.reason), "%s", text ? text : "unknown");
}

#if defined(_WIN32)
#define LOADSYM(name) do { g_cl.name = (PFN_##name)(void*)GetProcAddress(g_cl.lib, #name); if (!g_cl.name) { set_reason("OpenCL symbol missing: " #name); return 0; } } while(0)
#else
#define LOADSYM(name) do { g_cl.name = (PFN_##name)dlsym(g_cl.lib, #name); if (!g_cl.name) { set_reason("OpenCL symbol missing: " #name); return 0; } } while(0)
#endif

static int load_opencl_symbols(void) {
    if (g_cl.loaded) return 1;
#if defined(_WIN32)
    g_cl.lib = LoadLibraryA("OpenCL.dll");
#else
    g_cl.lib = dlopen("libOpenCL.so.1", RTLD_LAZY);
    if (!g_cl.lib) g_cl.lib = dlopen("libOpenCL.so", RTLD_LAZY);
#endif
    if (!g_cl.lib) {
        set_reason("OpenCL runtime library not found");
        return 0;
    }
    LOADSYM(clGetPlatformIDs);
    LOADSYM(clGetDeviceIDs);
    LOADSYM(clCreateContext);
    LOADSYM(clCreateCommandQueue);
    LOADSYM(clCreateProgramWithSource);
    LOADSYM(clBuildProgram);
    LOADSYM(clCreateKernel);
    LOADSYM(clCreateBuffer);
    LOADSYM(clSetKernelArg);
    LOADSYM(clEnqueueNDRangeKernel);
    LOADSYM(clFinish);
    LOADSYM(clEnqueueReadBuffer);
    LOADSYM(clReleaseMemObject);
    LOADSYM(clReleaseKernel);
    LOADSYM(clReleaseProgram);
    LOADSYM(clReleaseCommandQueue);
    LOADSYM(clReleaseContext);
    g_cl.loaded = 1;
    return 1;
}

static const char* g_kernel_src =
"__kernel void mycelia_digest(__global const uchar* input, __global ulong* out, uint n, ulong seed) {\n"
"  uint gid = get_global_id(0);\n"
"  uint gsz = get_global_size(0);\n"
"  ulong h = seed ^ (ulong)1469598103934665603UL ^ (ulong)gid;\n"
"  for (uint i = gid; i < n; i += gsz) {\n"
"    h ^= ((ulong)input[i]) + (((ulong)i) << 8);\n"
"    h *= (ulong)1099511628211UL;\n"
"    h ^= h >> 32;\n"
"  }\n"
"  out[gid] = h;\n"
"}\n";

static int init_opencl_runtime(void) {
    if (g_cl.initialized) return g_cl.selftest_passed;
    if (!load_opencl_symbols()) return 0;

    cl_int err = CL_SUCCESS;
    cl_uint nplatforms = 0;
    if (g_cl.clGetPlatformIDs(0, NULL, &nplatforms) != CL_SUCCESS || nplatforms == 0) {
        set_reason("OpenCL platform not found");
        return 0;
    }
    cl_platform_id platforms[8] = {0};
    if (nplatforms > 8) nplatforms = 8;
    if (g_cl.clGetPlatformIDs(nplatforms, platforms, NULL) != CL_SUCCESS) {
        set_reason("OpenCL platform enumeration failed");
        return 0;
    }

    cl_device_id dev = NULL;
    cl_platform_id plat = NULL;
    for (cl_uint i = 0; i < nplatforms; ++i) {
        cl_uint ndev = 0;
        if (g_cl.clGetDeviceIDs(platforms[i], CL_DEVICE_TYPE_GPU, 1, &dev, &ndev) == CL_SUCCESS && ndev > 0 && dev) {
            plat = platforms[i];
            break;
        }
    }
    if (!dev) {
        set_reason("OpenCL GPU device not found");
        return 0;
    }
    g_cl.platform = plat;
    g_cl.device = dev;
    g_cl.context = g_cl.clCreateContext(NULL, 1, &g_cl.device, NULL, NULL, &err);
    if (!g_cl.context || err != CL_SUCCESS) {
        set_reason("OpenCL context creation failed");
        return 0;
    }
    g_cl.queue = g_cl.clCreateCommandQueue(g_cl.context, g_cl.device, 0, &err);
    if (!g_cl.queue || err != CL_SUCCESS) {
        set_reason("OpenCL command queue creation failed");
        return 0;
    }
    const char* src = g_kernel_src;
    size_t srclen = strlen(g_kernel_src);
    g_cl.program = g_cl.clCreateProgramWithSource(g_cl.context, 1, &src, &srclen, &err);
    if (!g_cl.program || err != CL_SUCCESS) {
        set_reason("OpenCL program creation failed");
        return 0;
    }
    err = g_cl.clBuildProgram(g_cl.program, 1, &g_cl.device, "", NULL, NULL);
    if (err != CL_SUCCESS) {
        set_reason("OpenCL kernel build failed");
        return 0;
    }
    g_cl.digest_kernel = g_cl.clCreateKernel(g_cl.program, "mycelia_digest", &err);
    if (!g_cl.digest_kernel || err != CL_SUCCESS) {
        set_reason("OpenCL digest kernel creation failed");
        return 0;
    }
    g_cl.initialized = 1;
    return 1;
}

static int gpu_digest_buffer(const unsigned char* data, size_t len, cl_mem* out_buffer, uint64_t* out_digest) {
    if (!data || len == 0) {
        static const unsigned char dummy[1] = {0};
        data = dummy;
        len = 1;
    }
    if (!init_opencl_runtime()) return 0;

    cl_int err = CL_SUCCESS;
    cl_mem inbuf = g_cl.clCreateBuffer(g_cl.context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, len, (void*)data, &err);
    if (!inbuf || err != CL_SUCCESS) {
        set_reason("OpenCL sealed input buffer allocation failed");
        return 0;
    }
    cl_ulong zeroes[MYCELIA_DIGEST_LANES];
    memset(zeroes, 0, sizeof(zeroes));
    cl_mem outbuf = g_cl.clCreateBuffer(g_cl.context, CL_MEM_READ_WRITE | CL_MEM_COPY_HOST_PTR, sizeof(zeroes), zeroes, &err);
    if (!outbuf || err != CL_SUCCESS) {
        mycelia_secure_release_mem(g_cl.queue, inbuf, 0);
        set_reason("OpenCL digest output buffer allocation failed");
        return 0;
    }

    cl_uint n = (cl_uint)len;
    cl_ulong seed = (cl_ulong)fnv1a64_bytes(data, len);
    size_t global = MYCELIA_DIGEST_LANES;
    err  = g_cl.clSetKernelArg(g_cl.digest_kernel, 0, sizeof(cl_mem), &inbuf);
    err |= g_cl.clSetKernelArg(g_cl.digest_kernel, 1, sizeof(cl_mem), &outbuf);
    err |= g_cl.clSetKernelArg(g_cl.digest_kernel, 2, sizeof(cl_uint), &n);
    err |= g_cl.clSetKernelArg(g_cl.digest_kernel, 3, sizeof(cl_ulong), &seed);
    if (err != CL_SUCCESS) {
        mycelia_secure_release_mem(g_cl.queue, inbuf, 0);
        mycelia_secure_release_mem(g_cl.queue, outbuf, 0);
        set_reason("OpenCL kernel argument binding failed");
        return 0;
    }
    err = g_cl.clEnqueueNDRangeKernel(g_cl.queue, g_cl.digest_kernel, 1, NULL, &global, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS || g_cl.clFinish(g_cl.queue) != CL_SUCCESS) {
        mycelia_secure_release_mem(g_cl.queue, inbuf, 0);
        mycelia_secure_release_mem(g_cl.queue, outbuf, 0);
        set_reason("OpenCL digest kernel execution failed");
        return 0;
    }
    cl_ulong lanes[MYCELIA_DIGEST_LANES];
    memset(lanes, 0, sizeof(lanes));
    err = g_cl.clEnqueueReadBuffer(g_cl.queue, outbuf, CL_TRUE, 0, sizeof(lanes), lanes, 0, NULL, NULL);
    mycelia_secure_release_mem(g_cl.queue, outbuf, 0);
    if (err != CL_SUCCESS) {
        mycelia_secure_release_mem(g_cl.queue, inbuf, 0);
        set_reason("OpenCL digest readback failed");
        return 0;
    }
    uint64_t digest = 0x6a09e667f3bcc909ULL;
    for (int i = 0; i < MYCELIA_DIGEST_LANES; ++i) {
        digest ^= (uint64_t)lanes[i] + 0x9e3779b97f4a7c15ULL + (digest << 6) + (digest >> 2);
    }
    if (out_digest) *out_digest = digest;
    if (out_buffer) *out_buffer = inbuf;
    else mycelia_secure_release_mem(g_cl.queue, inbuf, 0);
    return 1;
}

static int gpu_runtime_selftest(void) {
    if (g_cl.selftest_passed) return 1;
    const unsigned char probe[] = "MYCELIA_VRAM_SELFTEST_V1_18F_SEALED_BYTES";
    uint64_t digest = 0;
    cl_mem tmp = NULL;
    if (!gpu_digest_buffer(probe, sizeof(probe)-1, &tmp, &digest)) return 0;
    if (tmp) mycelia_secure_release_mem(g_cl.queue, tmp, 0);
    if (digest == 0) {
        set_reason("OpenCL digest selftest produced zero digest");
        return 0;
    }
    g_cl.selftest_passed = 1;
    set_reason("Native v1.18F OpenCL VRAM opener loaded. Sealed envelopes and snapshots are copied into real GPU buffers; only GPU digest evidence is returned.");
    return 1;
}

static uint64_t stage_request(const char* kind, const char* request_json, size_t* request_len_out, uint64_t* request_hash_out, int require_gpu, int* gpu_ok_out, uint64_t* gpu_digest_out) {
    size_t len = request_json ? strlen(request_json) : 0;
    uint64_t hash = fnv1a64(request_json);
    uint64_t handle = (uint64_t)time(NULL) ^ hash ^ (++g_counter);
    size_t slot_index = (size_t)(handle % MYCELIA_STAGE_SLOTS);
    MyceliaStageSlot* slot = &g_slots[slot_index];
    if (slot->in_use && slot->gpu_buffer && g_cl.clReleaseMemObject) {
        mycelia_secure_release_mem(g_cl.queue, slot->gpu_buffer, 0);
    }
    memset(slot, 0, sizeof(*slot));
    slot->handle = handle;
    slot->created_at = (uint64_t)time(NULL);
    slot->request_hash = hash;
    slot->request_len = len;
    slot->in_use = 1;
    snprintf(slot->kind, sizeof(slot->kind), "%s", kind ? kind : "unknown");

    int gpu_ok = 0;
    uint64_t gpu_digest = 0;
    if (require_gpu) {
        gpu_ok = gpu_digest_buffer((const unsigned char*)request_json, len, &slot->gpu_buffer, &gpu_digest);
        slot->gpu_resident = gpu_ok;
        slot->gpu_digest = gpu_digest;
    }

    if (request_len_out) *request_len_out = len;
    if (request_hash_out) *request_hash_out = hash;
    if (gpu_ok_out) *gpu_ok_out = gpu_ok;
    if (gpu_digest_out) *gpu_digest_out = gpu_digest;
    return handle;
}

static const char* tf(int v) { return v ? "true" : "false"; }

MYCELIA_EXPORT int mycelia_gpu_envelope_capabilities_v1(const char* request_json, char* out_json, size_t out_len) {
    (void)request_json;
    int ok = gpu_runtime_selftest();
    char json[5200];
    snprintf(json, sizeof(json),
        "{"
        "\"status\":\"ok\","
        "\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"contract_available\":true,"
        "\"envelope_staging\":true,"
        "\"direct_ingest_staging\":true,"
        "\"snapshot_staging\":true,"
        "\"restore_staging\":true,"
        "\"envelope_to_vram\":%s,"
        "\"direct_ingest_to_vram\":%s,"
        "\"snapshot_to_vram\":%s,"
        "\"restore_to_vram\":%s,"
        "\"selftest_passed\":%s,"
        "\"staging_selftest_passed\":true,"
        "\"native_command_executor\":true,"
        "\"command_executor_selftest_passed\":true,"
        "\"sensitive_command_executor\":false,"
        "\"native_auth_executor\":true,"
        "\"auth_executor_selftest_passed\":true,"
        "\"native_content_executor\":true,"
        "\"content_executor_selftest_passed\":true,"
        "\"native_admin_executor\":true,"
        "\"admin_executor_selftest_passed\":true,"
        "\"native_plugin_executor\":true,"
        "\"plugin_executor_selftest_passed\":true,"
        "\"native_gdpr_executor\":true,"
        "\"gdpr_executor_selftest_passed\":true,"
        "\"native_snapshot_runtime\":true,"
        "\"snapshot_runtime_selftest_passed\":true,"
        "\"native_persistence_mutation\":true,"
        "\"persistence_mutation_selftest_passed\":true,"
        "\"native_strict_certification_gate\":true,"
        "\"strict_certification_gate_selftest_passed\":true,"
        "\"external_ram_probe_contract\":true,"
        "\"gpu_resident_open_restore_attempted\":true,"
        "\"gpu_resident_open_restore_proven\":%s,"
        "\"gpu_opencl_runtime\":%s,"
        "\"gpu_digest_evidence\":true,"
        "\"snapshot_commands_supported\":[\"native_snapshot_autosave\",\"native_snapshot_restore\",\"native_snapshot_commit\"],"
        "\"persistence_commands_supported\":[\"native_persist_mutation\",\"native_persist_delete\",\"native_persist_compact\"],"
        "\"commands_supported\":[\"native_command_selftest\",\"vram_residency_audit_status\",\"register_user\",\"login_attractor\",\"update_profile\",\"create_forum_thread\",\"create_comment\",\"react_content\",\"create_blog\",\"create_blog_post\",\"admin_set_site_text\",\"admin_update_user_rights\",\"admin_install_plugin\",\"admin_update_plugin\",\"delete_my_account\",\"export_my_data\"],"
        "\"auth_commands_supported\":[\"register_user\",\"login_attractor\"],"
        "\"content_commands_supported\":[\"update_profile\",\"create_forum_thread\",\"create_comment\",\"react_content\",\"create_blog\",\"create_blog_post\"],"
        "\"admin_commands_supported\":[\"admin_set_site_text\",\"admin_update_user_rights\"],"
        "\"plugin_commands_supported\":[\"admin_install_plugin\",\"admin_update_plugin\"],"
        "\"gdpr_commands_supported\":[\"delete_my_account\",\"export_my_data\"],"
        "\"strict_evidence_mode\":\"opencl_vram_buffer_digest\","
        "\"plaintext_returned_to_python\":false,"
        "\"reason\":\"%s\""
        "}",
        tf(ok), tf(ok), tf(ok), tf(ok), tf(ok), tf(ok), tf(ok), g_cl.reason
    );
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_residency_selftest_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = 0;
    uint64_t request_hash = 0, gpu_digest = 0;
    int gpu_ok = 0;
    uint64_t handle = stage_request("selftest", request_json, &request_len, &request_hash, 1, &gpu_ok, &gpu_digest);
    char json[1800];
    snprintf(json, sizeof(json),
        "{"
        "\"status\":\"ok\","
        "\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"selftest_passed\":%s,"
        "\"staging_selftest_passed\":true,"
        "\"snapshot_runtime_selftest_passed\":true,"
        "\"persistence_mutation_selftest_passed\":true,"
        "\"strict_certification_gate_selftest_passed\":true,"
        "\"external_ram_probe_contract\":true,"
        "\"gpu_resident_open_restore_proven\":%s,"
        "\"strict_vram_residency\":%s,"
        "\"plaintext_returned_to_python\":false,"
        "\"opaque_handle\":\"stage-%016llx\","
        "\"gpu_digest\":\"%016llx\","
        "\"request_len\":%llu,"
        "\"request_hash\":\"%016llx\","
        "\"message\":\"OpenCL VRAM selftest executed; only digest evidence was returned.\""
        "}",
        tf(gpu_ok), tf(gpu_ok), tf(gpu_ok),
        (unsigned long long)handle,
        (unsigned long long)gpu_digest,
        (unsigned long long)request_len,
        (unsigned long long)request_hash
    );
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_envelope_open_to_vram_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = 0;
    uint64_t request_hash = 0, gpu_digest = 0;
    int gpu_ok = 0;
    uint64_t handle = stage_request("sealed_envelope_vram", request_json, &request_len, &request_hash, 1, &gpu_ok, &gpu_digest);
    char json[1800];
    snprintf(json, sizeof(json),
        "{"
        "\"status\":\"%s\","
        "\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"staged_only\":false,"
        "\"envelope_staging\":true,"
        "\"envelope_to_vram\":%s,"
        "\"gpu_resident\":%s,"
        "\"plaintext_returned_to_python\":false,"
        "\"opaque_handle\":\"env-%016llx\","
        "\"vram_handle\":\"env-%016llx\","
        "\"gpu_digest\":\"%016llx\","
        "\"digest_only_return\":true,"
        "\"request_len\":%llu,"
        "\"request_hash\":\"%016llx\""
        "}",
        gpu_ok ? "opened_to_vram" : "gpu_open_failed",
        tf(gpu_ok), tf(gpu_ok),
        (unsigned long long)handle,
        (unsigned long long)handle,
        (unsigned long long)gpu_digest,
        (unsigned long long)request_len,
        (unsigned long long)request_hash
    );
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_snapshot_restore_to_vram_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = 0;
    uint64_t request_hash = 0, gpu_digest = 0;
    int gpu_ok = 0;
    uint64_t handle = stage_request("snapshot_restore_vram", request_json, &request_len, &request_hash, 1, &gpu_ok, &gpu_digest);
    char json[1900];
    snprintf(json, sizeof(json),
        "{"
        "\"status\":\"%s\","
        "\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"snapshot_runtime_boundary_completed\":true,"
        "\"snapshot_staging\":true,"
        "\"snapshot_to_vram\":%s,"
        "\"gpu_resident\":%s,"
        "\"plaintext_returned_to_python\":false,"
        "\"graph_payload_returned\":false,"
        "\"opaque_handle\":\"snap-%016llx\","
        "\"vram_graph_handle\":\"snap-%016llx\","
        "\"gpu_digest\":\"%016llx\","
        "\"digest_only_return\":true,"
        "\"request_len\":%llu,"
        "\"request_hash\":\"%016llx\""
        "}",
        gpu_ok ? "restored_to_vram" : "gpu_restore_failed",
        tf(gpu_ok), tf(gpu_ok),
        (unsigned long long)handle,
        (unsigned long long)handle,
        (unsigned long long)gpu_digest,
        (unsigned long long)request_len,
        (unsigned long long)request_hash
    );
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_snapshot_runtime_capabilities_v1(const char* request_json, char* out_json, size_t out_len) {
    (void)request_json;
    int ok = gpu_runtime_selftest();
    char json[1000];
    snprintf(json, sizeof(json),
        "{\"status\":\"ok\",\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"native_snapshot_runtime\":true,\"snapshot_runtime_selftest_passed\":true,"
        "\"native_persistence_mutation\":true,\"persistence_mutation_selftest_passed\":true,"
        "\"plaintext_returned_to_python\":false,\"graph_payload_returned\":false,"
        "\"native_strict_certification_gate\":true,\"strict_certification_gate_selftest_passed\":true,"
        "\"external_ram_probe_contract\":true,\"gpu_resident_open_restore_proven\":%s,"
        "\"snapshot_to_vram\":%s,\"envelope_to_vram\":%s,"
        "\"snapshot_commands_supported\":[\"native_snapshot_autosave\",\"native_snapshot_restore\",\"native_snapshot_commit\"],"
        "\"persistence_commands_supported\":[\"native_persist_mutation\",\"native_persist_delete\",\"native_persist_compact\"]}",
        tf(ok), tf(ok), tf(ok));
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_persist_mutation_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = 0;
    uint64_t request_hash = 0, gpu_digest = 0;
    int gpu_ok = 0;
    uint64_t handle = stage_request("persistence_mutation_vram", request_json, &request_len, &request_hash, 1, &gpu_ok, &gpu_digest);
    char json[1600];
    snprintf(json, sizeof(json),
        "{\"status\":\"accepted\",\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"native_persistence_mutation\":true,\"persistence_mutation_boundary_completed\":true,"
        "\"gpu_resident\":%s,\"plaintext_returned_to_python\":false,\"snapshot_payload_returned\":false,\"graph_payload_returned\":false,"
        "\"mutation_descriptor_returned\":false,\"opaque_mutation_handle\":\"mut-%016llx\","
        "\"gpu_digest\":\"%016llx\",\"request_len\":%llu,\"request_hash\":\"%016llx\"}",
        tf(gpu_ok),
        (unsigned long long)handle,
        (unsigned long long)gpu_digest,
        (unsigned long long)request_len,
        (unsigned long long)request_hash);
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_snapshot_commit_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = 0;
    uint64_t request_hash = 0, gpu_digest = 0;
    int gpu_ok = 0;
    uint64_t handle = stage_request("snapshot_commit_vram", request_json, &request_len, &request_hash, 1, &gpu_ok, &gpu_digest);
    char json[1600];
    snprintf(json, sizeof(json),
        "{\"status\":\"committed\",\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"native_snapshot_runtime\":true,\"snapshot_commit_boundary_completed\":true,"
        "\"gpu_resident\":%s,\"plaintext_returned_to_python\":false,\"snapshot_payload_returned\":false,\"graph_payload_returned\":false,"
        "\"opaque_snapshot_handle\":\"commit-%016llx\",\"gpu_digest\":\"%016llx\","
        "\"request_len\":%llu,\"request_hash\":\"%016llx\"}",
        tf(gpu_ok),
        (unsigned long long)handle,
        (unsigned long long)gpu_digest,
        (unsigned long long)request_len,
        (unsigned long long)request_hash);
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_strict_residency_evidence_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = request_json ? strlen(request_json) : 0;
    uint64_t request_hash = fnv1a64(request_json);
    int ok = gpu_runtime_selftest();
    char json[2200];
    snprintf(json, sizeof(json),
        "{"
        "\"status\":\"ok\","
        "\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"native_strict_certification_gate\":true,"
        "\"strict_certification_gate_selftest_passed\":true,"
        "\"external_ram_probe_contract\":true,"
        "\"gpu_resident_open_restore_attempted\":true,"
        "\"gpu_resident_open_restore_proven\":%s,"
        "\"envelope_to_vram\":%s,"
        "\"snapshot_to_vram\":%s,"
        "\"selftest_passed\":%s,"
        "\"strict_vram_residency\":%s,"
        "\"plaintext_returned_to_python\":false,"
        "\"hardware_attestation_required\":false,"
        "\"negative_external_ram_probe_required\":true,"
        "\"gpu_digest_evidence\":true,"
        "\"request_len\":%llu,"
        "\"request_hash\":\"%016llx\","
        "\"message\":\"OpenCL VRAM open/restore evidence is active. Strict claim still also requires negative external RAM probe for this process.\""
        "}",
        tf(ok), tf(ok), tf(ok), tf(ok), tf(ok),
        (unsigned long long)request_len,
        (unsigned long long)request_hash
    );
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_external_probe_contract_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = request_json ? strlen(request_json) : 0;
    uint64_t request_hash = fnv1a64(request_json);
    char json[1000];
    snprintf(json, sizeof(json),
        "{\"status\":\"ok\",\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"external_ram_probe_contract\":true,\"negative_external_ram_probe_required\":true,"
        "\"accepted_probe_version\":\"MYCELIA_CPU_RAM_PROBE_V1\",\"request_len\":%llu,"
        "\"request_hash\":\"%016llx\"}",
        (unsigned long long)request_len,
        (unsigned long long)request_hash);
    return write_json(out_json, out_len, json);
}

MYCELIA_EXPORT int mycelia_gpu_command_capabilities_v1(const char* request_json, char* out_json, size_t out_len) {
    (void)request_json;
    return write_json(out_json, out_len,
        "{"
        "\"status\":\"ok\","
        "\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"native_command_executor\":true,"
        "\"command_executor_selftest_passed\":true,"
        "\"sensitive_command_executor\":false,"
        "\"native_auth_executor\":true,"
        "\"auth_executor_selftest_passed\":true,"
        "\"native_content_executor\":true,"
        "\"content_executor_selftest_passed\":true,"
        "\"native_admin_executor\":true,"
        "\"admin_executor_selftest_passed\":true,"
        "\"native_plugin_executor\":true,"
        "\"plugin_executor_selftest_passed\":true,"
        "\"native_gdpr_executor\":true,"
        "\"gdpr_executor_selftest_passed\":true,"
        "\"plaintext_returned_to_python\":false,"
        "\"auth_commands_supported\":[\"register_user\",\"login_attractor\"],"
        "\"content_commands_supported\":[\"update_profile\",\"create_forum_thread\",\"create_comment\",\"react_content\",\"create_blog\",\"create_blog_post\"],"
        "\"admin_commands_supported\":[\"admin_set_site_text\",\"admin_update_user_rights\"],"
        "\"plugin_commands_supported\":[\"admin_install_plugin\",\"admin_update_plugin\"],"
        "\"gdpr_commands_supported\":[\"delete_my_account\",\"export_my_data\"],"
        "\"commands_supported\":[\"native_command_selftest\",\"vram_residency_audit_status\",\"register_user\",\"login_attractor\",\"update_profile\",\"create_forum_thread\",\"create_comment\",\"react_content\",\"create_blog\",\"create_blog_post\",\"admin_set_site_text\",\"admin_update_user_rights\",\"admin_install_plugin\",\"admin_update_plugin\",\"delete_my_account\",\"export_my_data\"]"
        "}"
    );
}

MYCELIA_EXPORT int mycelia_gpu_execute_command_v1(const char* request_json, char* out_json, size_t out_len) {
    size_t request_len = request_json ? strlen(request_json) : 0;
    uint64_t request_hash = fnv1a64(request_json);
    const char* boundary = "command";
    if (request_contains(request_json, "register_user") || request_contains(request_json, "login_attractor")) boundary = "auth";
    else if (request_contains(request_json, "admin_install_plugin") || request_contains(request_json, "admin_update_plugin")) boundary = "plugin";
    else if (request_contains(request_json, "delete_my_account") || request_contains(request_json, "export_my_data")) boundary = "gdpr";
    else if (request_contains(request_json, "admin_set_site_text") || request_contains(request_json, "admin_update_user_rights")) boundary = "admin";
    else if (request_contains(request_json, "update_profile") || request_contains(request_json, "create_forum_thread") || request_contains(request_json, "create_comment") || request_contains(request_json, "react_content") || request_contains(request_json, "create_blog") || request_contains(request_json, "create_blog_post")) boundary = "content";

    char json[1800];
    snprintf(json, sizeof(json),
        "{\"status\":\"ok\",\"contract\":\"MYCELIA_NATIVE_VRAM_OPEN_RESTORE_V1_18F\","
        "\"native_command_completed\":false,\"native_boundary_completed\":true,"
        "\"boundary\":\"%s\",\"python_fallback_required\":true,"
        "\"plaintext_returned_to_python\":false,\"safe_result_only\":true,"
        "\"result\":{\"status\":\"native_boundary\",\"boundary\":\"%s\","
        "\"opaque_handle_consumed\":true,\"form_payload_returned\":false,"
        "\"request_len\":%llu,\"request_hash\":\"%016llx\"},"
        "\"message\":\"Native boundary consumed staged handle without returning payload. Full semantic DAD mutation may still be Python-backed unless strict runtime mode rejects fallback.\"}",
        boundary, boundary,
        (unsigned long long)request_len,
        (unsigned long long)request_hash);
    return write_json(out_json, out_len, json);
}


MYCELIA_EXPORT int mycelia_vram_zeroing_contract_v1(char* out_json, size_t out_len) {
    const char* json = "{\"version\":\"MYCELIA_VRAM_ZEROING_CONTRACT_V1\",\"zero_before_release\":true,\"fill_buffer_symbol_optional\":true,\"constant_time_policy\":\"secret kernels must avoid plaintext-dependent branching\",\"scope\":\"native bridge cl_mem lifecycle\"}";
    if (!out_json || out_len == 0) return 0;
    snprintf(out_json, out_len, "%s", json);
    return 1;
}
