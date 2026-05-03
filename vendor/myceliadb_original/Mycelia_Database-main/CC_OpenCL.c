/**
 * @file opencl_driver.c
 * @brief C implementation of an OpenCL driver providing GPU compute capabilities.
 *
 * This file contains the C interface for interacting with an OpenCL-capable GPU.
 * It includes functions for initialization, memory management, data transfer,
 * kernel compilation, and execution of various computational kernels commonly
 * used in deep learning (matrix multiplication, activations, normalization, etc.),
 * including specialized kernels for prototype-based models and spiking elements.
 *
 * The driver is designed to be compiled into a shared library (DLL/SO)
 * and called from a higher-level language (like Python).
 *
 * This version is adapted to be compatible with OpenCL 1.2, 2.x, and 3.x runtimes,
 * preferring modern API calls where available but maintaining compatibility.
 * It specifically handles the conditional compilation of kernels requiring atomics.
 * Includes loss shaping functionality based on a list of critical pairs.
 */

#define _CRT_SECURE_NO_WARNINGS /* For Visual Studio (if sprintf/sprintf_s is used) */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <math.h>
#include <float.h> /* For FLT_MAX, HUGE_VALF */
#include <stdint.h> // For uintptr_t
#include <stdarg.h>
#include <stdbool.h>
#include <limits.h>
#include <ctype.h>
#ifndef _WIN32
#include <strings.h>
#endif

#ifdef _WIN32
#include <windows.h>
#define cc_strncasecmp _strnicmp
#else
#include <pthread.h>
#define cc_strncasecmp strncasecmp
#endif

// Include OpenCL headers based on the operating system
#ifdef __APPLE__
#include <OpenCL/cl.h>
#else
#include <CL/cl.h>
#endif

#include "CipherCore_NoiseCtrl.h"
#include "SymBio_Interface.h"

// ---------------------------------------------------------------------------
// GPU Rendering data structures shared with Python
// ---------------------------------------------------------------------------

typedef struct {
    float x;
    float y;
} Vec2f;

typedef struct {
    float pos_x;
    float pos_y;
    float hue;
    int trail_start;
    int trail_len;
} RenderAgent;

// ---------------------------------------------------------------------------
// Mycel / Pheromone host-side state (emulation for DLL integration)
// ---------------------------------------------------------------------------

typedef struct MycelState {
    bool   initialized;
    int    T_cap;
    int    C;
    int    K;
    int    T_act;

    float* pheromone;     // [T_cap * K * C]
    int*   neigh_idx;     // [T_cap * K]
    float* decay;         // [T_cap * K]
    float* diffu;         // [T_cap * K]

    float* nutrient;      // [T_cap]
    float* mood;          // [T_cap * C]
    uint8_t* colony_id;   // [T_cap]
    uint8_t* alive;       // [T_cap]
    float* potential;     // [T_cap]
    float* subqg_field;   // [T_cap]

    int*   free_list;     // stack for inactive indices
    int    free_head;     // points to next free slot in stack

    float* reinforce_gain; // [C]
    float* kappa_mood;     // [C]
    float  kappa_nutrient;

    float  repro_thr_nutrient;
    float  repro_thr_activity;
    float  repro_mut_sigma;

    float  decay_default;
    float  diffu_default;
    float  nutrient_recovery;

    cl_mem pheromone_buf;
    cl_mem neigh_idx_buf;
    cl_mem decay_buf;
    cl_mem diffu_buf;
    cl_mem nutrient_buf;
    cl_mem mood_buf;
    cl_mem alive_buf;
    cl_mem reinforce_gain_buf;
} MycelState;

static MycelState g_mycel_state = {0};

// ---------------------------------------------------------------------------
// CPU fallback renderer helpers
// ---------------------------------------------------------------------------

static inline float clamp01f(float v) {
    if (v < 0.0f) { return 0.0f; }
    if (v > 1.0f) { return 1.0f; }
    return v;
}

static inline int clamp_int(int v, int min_v, int max_v) {
    if (v < min_v) { return min_v; }
    if (v > max_v) { return max_v; }
    return v;
}

static void hue_to_rgb(float hue, float* r, float* g, float* b) {
    float h = hue - floorf(hue);
    float s = 1.0f;
    float v = 1.0f;
    float c = v * s;
    float x = c * (1.0f - fabsf(fmodf(h * 6.0f, 2.0f) - 1.0f));
    float m = v - c;

    float rr = 0.0f, gg = 0.0f, bb = 0.0f;
    if (h < (1.0f / 6.0f)) {
        rr = c; gg = x; bb = 0.0f;
    } else if (h < (2.0f / 6.0f)) {
        rr = x; gg = c; bb = 0.0f;
    } else if (h < (3.0f / 6.0f)) {
        rr = 0.0f; gg = c; bb = x;
    } else if (h < (4.0f / 6.0f)) {
        rr = 0.0f; gg = x; bb = c;
    } else if (h < (5.0f / 6.0f)) {
        rr = x; gg = 0.0f; bb = c;
    } else {
        rr = c; gg = 0.0f; bb = x;
    }

    if (r) { *r = clamp01f(rr + m); }
    if (g) { *g = clamp01f(gg + m); }
    if (b) { *b = clamp01f(bb + m); }
}

static void blend_pixel(uint8_t* pixel, float r, float g, float b, float alpha) {
    float dst_r = (float)pixel[0] / 255.0f;
    float dst_g = (float)pixel[1] / 255.0f;
    float dst_b = (float)pixel[2] / 255.0f;

    float inv_a = 1.0f - clamp01f(alpha);
    dst_r = dst_r * inv_a + r * alpha;
    dst_g = dst_g * inv_a + g * alpha;
    dst_b = dst_b * inv_a + b * alpha;

    pixel[0] = (uint8_t)(clamp01f(dst_r) * 255.0f + 0.5f);
    pixel[1] = (uint8_t)(clamp01f(dst_g) * 255.0f + 0.5f);
    pixel[2] = (uint8_t)(clamp01f(dst_b) * 255.0f + 0.5f);
    pixel[3] = 255;
}

static float sample_subqg_height(const float* subqg_field,
                                 int width,
                                 int height,
                                 int total_cells,
                                 int x,
                                 int y) {
    if (!subqg_field || width <= 0 || height <= 0 || total_cells <= 0) {
        return 0.0f;
    }

    if (x < 0) { x = 0; }
    if (x >= width) { x = width - 1; }
    if (y < 0) { y = 0; }
    if (y >= height) { y = height - 1; }

    size_t idx = (size_t)y * (size_t)width + (size_t)x;
    if (idx >= (size_t)total_cells) {
        return 0.0f;
    }
    return subqg_field[idx];
}

static void draw_disc(uint8_t* buffer, int width, int height,
                      float cx, float cy, float radius,
                      float r, float g, float b, float alpha) {
    if (!buffer || width <= 0 || height <= 0 || radius <= 0.0f) {
        return;
    }

    float radius_sq = radius * radius;
    int min_x = clamp_int((int)floorf(cx - radius - 1.0f), 0, width - 1);
    int max_x = clamp_int((int)ceilf (cx + radius + 1.0f), 0, width - 1);
    int min_y = clamp_int((int)floorf(cy - radius - 1.0f), 0, height - 1);
    int max_y = clamp_int((int)ceilf (cy + radius + 1.0f), 0, height - 1);

    for (int y = min_y; y <= max_y; ++y) {
        for (int x = min_x; x <= max_x; ++x) {
            float dx = (float)x + 0.5f - cx;
            float dy = (float)y + 0.5f - cy;
            float dist_sq = dx * dx + dy * dy;
            if (dist_sq <= radius_sq) {
                uint8_t* pixel = &buffer[((size_t)y * (size_t)width + (size_t)x) * 4];
                float falloff = 1.0f - clamp01f(dist_sq / radius_sq);
                blend_pixel(pixel, r, g, b, alpha * falloff);
            }
        }
    }
}

static void draw_line(uint8_t* buffer, int width, int height,
                      float x0, float y0, float x1, float y1,
                      float radius, float r, float g, float b, float alpha) {
    if (!buffer || width <= 0 || height <= 0) {
        return;
    }

    float dx = x1 - x0;
    float dy = y1 - y0;
    float length = sqrtf(dx * dx + dy * dy);
    int steps = (int)ceilf(length);
    if (steps < 1) {
        steps = 1;
    }

    float inv_steps = 1.0f / (float)steps;
    for (int i = 0; i <= steps; ++i) {
        float t = (float)i * inv_steps;
        float cx = x0 + dx * t;
        float cy = y0 + dy * t;
        draw_disc(buffer, width, height, cx, cy, radius, r, g, b, alpha);
    }
}

static void to_pixel_coords(const Vec2f* in, int width, int height, float* out_x, float* out_y) {
    if (!in || !out_x || !out_y) {
        return;
    }
    float px = in->x;
    float py = in->y;
    if (width > 1 && height > 1 && px >= 0.0f && px <= 1.0f && py >= 0.0f && py <= 1.0f) {
        px *= (float)(width - 1);
        py *= (float)(height - 1);
    }
    *out_x = px;
    *out_y = py;
}

static void render_frame_cpu(const struct MycelState* state,
                             uint8_t* out_buffer,
                             int width, int height,
                             const RenderAgent* agents, int num_agents,
                             const Vec2f* trail_points, int num_trail_points,
                             float exposure_scale, float agent_radius,
                             float trail_thickness, float clip_percentile) {
    if (!out_buffer || width <= 0 || height <= 0) {
        return;
    }

    int neighbor_count = (state && state->K > 0) ? state->K : 0;
    int channel_count = (state && state->C > 0) ? state->C : 0;
    int total_cells = state ? state->T_cap : 0;
    int active_cells = state ? state->T_act : 0;
    int field_cells = total_cells;
    if (active_cells > 0 && active_cells < field_cells) {
        field_cells = active_cells;
    }
    const float* pheromone = (state) ? state->pheromone : NULL;
    const float* subqg_field = (state) ? state->subqg_field : NULL;

    float clip_norm = clip_percentile;
    if (clip_norm > 1.0f) {
        clip_norm *= 0.01f;
    }
    clip_norm = clamp01f(clip_norm);
    if (clip_norm <= 0.0f) {
        clip_norm = 1.0f;
    }

    float base_bias = clamp01f(0.02f + 0.04f * exposure_scale);
    float inv_neighbors = (neighbor_count > 0) ? (1.0f / (float)neighbor_count) : 1.0f;
    float inv_exposure = (exposure_scale > 1e-5f) ? (1.0f / exposure_scale) : 1.0f;

    const float deep_r = 0.050f, deep_g = 0.090f, deep_b = 0.200f;
    const float shallow_r = 0.350f, shallow_g = 0.650f, shallow_b = 0.950f;
    const float light_dir_x = -0.45f, light_dir_y = -0.55f, light_dir_z = 0.70f;
    float light_len = sqrtf(light_dir_x * light_dir_x + light_dir_y * light_dir_y + light_dir_z * light_dir_z);
    if (light_len < 1e-6f) { light_len = 1.0f; }
    float lx = light_dir_x / light_len;
    float ly = light_dir_y / light_len;
    float lz = light_dir_z / light_len;
    float hvx = lx;
    float hvy = ly;
    float hvz = lz + 1.0f;  // view dir is (0,0,1)
    float hv_len = sqrtf(hvx * hvx + hvy * hvy + hvz * hvz);
    if (hv_len < 1e-6f) { hv_len = 1.0f; }
    hvx /= hv_len;
    hvy /= hv_len;
    hvz /= hv_len;

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            size_t cell_idx = (size_t)y * (size_t)width + (size_t)x;
            float height_center = sample_subqg_height(subqg_field, width, height, field_cells, x, y);
            float height_norm = tanhf(height_center * 0.35f);
            float grad_x = 0.5f * (sample_subqg_height(subqg_field, width, height, field_cells, x + 1, y)
                                   - sample_subqg_height(subqg_field, width, height, field_cells, x - 1, y));
            float grad_y = 0.5f * (sample_subqg_height(subqg_field, width, height, field_cells, x, y + 1)
                                   - sample_subqg_height(subqg_field, width, height, field_cells, x, y - 1));

            float nx = -grad_x * 3.2f;
            float ny = -grad_y * 3.2f;
            float nz = 1.0f;
            float inv_len = 1.0f / sqrtf(nx * nx + ny * ny + nz * nz + 1e-6f);
            nx *= inv_len;
            ny *= inv_len;
            nz *= inv_len;

            float diffuse = fmaxf(0.0f, nx * lx + ny * ly + nz * lz);
            float spec = powf(fmaxf(0.0f, nx * hvx + ny * hvy + nz * hvz), 48.0f);
            float grad_mag = sqrtf(grad_x * grad_x + grad_y * grad_y);
            float foam = clamp01f(grad_mag * 2.2f + fmaxf(0.0f, height_norm - 0.6f) * 1.8f);
            float mix_t = clamp01f(0.5f + 0.5f * height_norm);
            float base_r = deep_r * (1.0f - mix_t) + shallow_r * mix_t;
            float base_g = deep_g * (1.0f - mix_t) + shallow_g * mix_t;
            float base_b = deep_b * (1.0f - mix_t) + shallow_b * mix_t;
            float lighting = 0.25f + diffuse * 0.9f;

            float color_r = base_r * lighting + spec * 0.40f;
            float color_g = base_g * lighting + spec * 0.40f;
            float color_b = base_b * lighting + spec * 0.40f;

            color_r += foam * 0.25f;
            color_g += foam * 0.32f;
            color_b += foam * 0.35f;

            float pher_r = 0.0f;
            float pher_g = 0.0f;
            float pher_b = 0.0f;

            if (pheromone && channel_count > 0 && neighbor_count > 0 &&
                cell_idx < (size_t)total_cells) {
                size_t edge_base = cell_idx * (size_t)neighbor_count;
                for (int k = 0; k < neighbor_count; ++k) {
                    size_t edge_idx = edge_base + (size_t)k;
                    size_t channel_base = edge_idx * (size_t)channel_count;
                    if (channel_count >= 1) {
                        pher_r += fabsf(pheromone[channel_base + 0]);
                    }
                    if (channel_count >= 2) {
                        pher_g += fabsf(pheromone[channel_base + 1]);
                    }
                    if (channel_count >= 3) {
                        pher_b += fabsf(pheromone[channel_base + 2]);
                    }
                }

                pher_r *= inv_neighbors * inv_exposure;
                pher_g *= inv_neighbors * inv_exposure;
                pher_b *= inv_neighbors * inv_exposure;
            }

            pher_r = fminf(pher_r, clip_norm);
            pher_g = fminf(pher_g, clip_norm);
            pher_b = fminf(pher_b, clip_norm);

            color_r = clamp01f(color_r + pher_r * 0.12f + base_bias);
            color_g = clamp01f(color_g + pher_g * 0.12f + base_bias);
            color_b = clamp01f(color_b + pher_b * 0.12f + base_bias);

            uint8_t* pixel = &out_buffer[(cell_idx * 4) + 0];
            pixel[0] = (uint8_t)(color_r * 255.0f + 0.5f);
            pixel[1] = (uint8_t)(color_g * 255.0f + 0.5f);
            pixel[2] = (uint8_t)(color_b * 255.0f + 0.5f);
            pixel[3] = 255;
        }
    }

    float px_agent_radius = agent_radius;
    float px_trail_radius = trail_thickness;
    float scale_hint = (float)((width < height) ? width : height);
    if (!isfinite(px_agent_radius) || px_agent_radius <= 0.0f) {
        px_agent_radius = 0.0125f * scale_hint;
    } else if (px_agent_radius <= 2.0f) {
        px_agent_radius *= scale_hint;
    }
    if (!isfinite(px_trail_radius) || px_trail_radius <= 0.0f) {
        px_trail_radius = 0.006f * scale_hint;
    } else if (px_trail_radius <= 2.0f) {
        px_trail_radius *= scale_hint;
    }

    px_agent_radius = fmaxf(1.25f, px_agent_radius);
    px_trail_radius = fmaxf(0.75f, px_trail_radius);

    float trail_alpha = clamp01f(0.25f + 0.15f * exposure_scale);
    float agent_alpha = clamp01f(0.7f + 0.2f * exposure_scale);

    for (int i = 0; i < num_agents; ++i) {
        const RenderAgent* agent = &agents[i];
        float hue = agent->hue;
        float r = 0.0f, g = 0.0f, b = 0.0f;
        hue_to_rgb(hue, &r, &g, &b);

        if (trail_points && num_trail_points > 0 && agent->trail_len > 1) {
            int start = agent->trail_start;
            int end = start + agent->trail_len - 1;
            if (start < 0) { start = 0; }
            if (end >= num_trail_points) { end = num_trail_points - 1; }
            Vec2f prev = trail_points[start];
            float prev_x, prev_y;
            to_pixel_coords(&prev, width, height, &prev_x, &prev_y);
            for (int idx = start + 1; idx <= end; ++idx) {
                Vec2f curr = trail_points[idx];
                float curr_x, curr_y;
                to_pixel_coords(&curr, width, height, &curr_x, &curr_y);
                draw_line(out_buffer, width, height,
                          prev_x, prev_y, curr_x, curr_y,
                          px_trail_radius, r, g, b, trail_alpha * 0.6f);
                prev_x = curr_x;
                prev_y = curr_y;
            }
        }

        float agent_x, agent_y;
        Vec2f pos = {agent->pos_x, agent->pos_y};
        to_pixel_coords(&pos, width, height, &agent_x, &agent_y);
        draw_disc(out_buffer, width, height, agent_x, agent_y,
                  px_agent_radius, r, g, b, agent_alpha);
    }
}

static cl_int set_render_kernel_args(
    cl_kernel kernel,
    cl_mem out_target,
    cl_mem agents_buf,
    cl_int n_agents,
    cl_mem trails_buf,
    cl_int n_trails,
    cl_int width,
    cl_int height,
    cl_float exposure,
    cl_float agent_radius,
    cl_float trail_thickness,
    cl_float clip_value,
    cl_mem pheromone_buf,
    cl_int total_cells,
    cl_int active_cells,
    cl_int neighbor_count,
    cl_int channel_count,
    cl_mem subqg_field_buf,
    cl_int subqg_len,
    cl_int subqg_w,
    cl_int subqg_h)
{
    if (!kernel) {
        return CL_INVALID_KERNEL;
    }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err = clSetKernelArg(kernel, arg++, sizeof(cl_mem), &out_target);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_mem), &agents_buf);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &n_agents);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_mem), &trails_buf);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &n_trails);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &width);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &height);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_float), &exposure);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_float), &agent_radius);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_float), &trail_thickness);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_float), &clip_value);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_mem), &pheromone_buf);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &total_cells);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &active_cells);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &neighbor_count);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &channel_count);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_mem), &subqg_field_buf);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &subqg_len);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &subqg_w);
    if (err != CL_SUCCESS) { return err; }
    err = clSetKernelArg(kernel, arg++, sizeof(cl_int), &subqg_h);
    if (err != CL_SUCCESS) { return err; }
    return CL_SUCCESS;
}

/**
 * @brief Returns a human-readable string for an OpenCL error code.
 * Maps standard OpenCL error codes (negative integers) to descriptive strings.
 * @param error The cl_int error code.
 * @return A constant C string describing the error. Returns "Unknown OpenCL error %d" if the code is not recognized.
 */
const char* clGetErrorString(cl_int error) {
    /* Static map of error codes to strings (standard OpenCL errors) */
    static const char *errStr[] = {
        "CL_SUCCESS", "CL_DEVICE_NOT_FOUND", "CL_DEVICE_NOT_AVAILABLE", "CL_COMPILER_NOT_AVAILABLE",
        "CL_MEM_OBJECT_ALLOCATION_FAILURE", "CL_OUT_OF_RESOURCES", "CL_OUT_OF_HOST_MEMORY",
        "CL_PROFILING_INFO_NOT_AVAILABLE", "CL_MEM_COPY_OVERLAP", "CL_IMAGE_FORMAT_MISMATCH",
        "CL_IMAGE_FORMAT_NOT_SUPPORTED", "CL_BUILD_PROGRAM_FAILURE", "CL_MAP_FAILURE",
        /* Placeholder for codes -13 to -29 */
        "CL_MISALIGNED_SUB_BUFFER_OFFSET", "CL_EXEC_STATUS_ERROR_FOR_EVENTS_IN_WAIT_LIST", "CL_COMPILE_PROGRAM_FAILURE",
        "CL_LINKER_NOT_AVAILABLE", "CL_LINK_PROGRAM_FAILURE", "CL_DEVICE_PARTITION_FAILED", "CL_KERNEL_ARG_INFO_NOT_AVAILABLE",
        "", "", "", "", "", "", "", "", "", "",
        "CL_INVALID_VALUE", "CL_INVALID_DEVICE_TYPE", "CL_INVALID_PLATFORM", "CL_INVALID_DEVICE", "CL_INVALID_CONTEXT",
        "CL_INVALID_QUEUE_PROPERTIES", "CL_INVALID_COMMAND_QUEUE", "CL_INVALID_HOST_PTR", "CL_INVALID_MEM_OBJECT",
        "CL_INVALID_IMAGE_FORMAT_DESCRIPTOR", "CL_INVALID_IMAGE_SIZE", "CL_INVALID_SAMPLER", "CL_INVALID_BINARY",
        "CL_INVALID_BUILD_OPTIONS", "CL_INVALID_PROGRAM", "CL_INVALID_PROGRAM_EXECUTABLE", "CL_INVALID_KERNEL_NAME",
        "CL_INVALID_KERNEL_DEFINITION", "CL_INVALID_KERNEL", "CL_INVALID_ARG_INDEX", "CL_INVALID_ARG_VALUE",
        "CL_INVALID_ARG_SIZE", "CL_INVALID_KERNEL_ARGS", "CL_INVALID_WORK_DIMENSION", "CL_INVALID_WORK_GROUP_SIZE",
        "CL_INVALID_WORK_ITEM_SIZE", "CL_INVALID_GLOBAL_OFFSET", "CL_INVALID_EVENT_WAIT_LIST", "CL_INVALID_EVENT",
        "CL_INVALID_OPERATION", "CL_INVALID_GL_OBJECT", "CL_INVALID_BUFFER_SIZE", "CL_INVALID_MIP_LEVEL",
        "CL_INVALID_GLOBAL_WORK_SIZE", "CL_INVALID_PROPERTY", "CL_INVALID_IMAGE_DESCRIPTOR", "CL_INVALID_COMPILER_OPTIONS",
        "CL_INVALID_LINKER_OPTIONS", "CL_INVALID_DEVICE_PARTITION_COUNT",
        /* Add more specific error codes for newer OpenCL versions if needed */
        "CL_INVALID_PIPE_SIZE", "CL_INVALID_DEVICE_QUEUE" /* Examples for 2.0+ */
    };
    const int errCount = (int)(sizeof(errStr) / sizeof(errStr[0]));
    const int index = -error; /* Error codes are negative integers */

    /* Check if the index is within the bounds of our static map */
    if (index >= 0 && index < errCount) {
        const char* err = errStr[index];
        /* Return the string if it's valid and not empty */
        if (err && err[0] != '\0') {
            return err;
        }
    }
    /* If the error code is unknown or the string is empty, return a generic message */
    static char unknown_error[64];
    /* Use snprintf (C99) for better portability and safety */
    snprintf(unknown_error, sizeof(unknown_error), "Unknown OpenCL error %d", error);
    unknown_error[sizeof(unknown_error) - 1] = '\0'; /* Ensure null termination */
    return unknown_error;
}

// --- Platform Specific Defines ---
#ifndef M_PI
/** @brief Definition of PI if not already defined. */
#define M_PI 3.14159265358979323846
#endif
/** @brief Constant 1/sqrt(2*pi), used in GELU backward calculation. */
#define M_1_SQRT2PI 0.39894228040143267794f

/**
 * @brief Defines the floating-point type used within the OpenCL kernels (e.g., float, half).
 * Affects kernel compilation options and buffer sizes.
 */
#define KERNEL_FP_TYPE float
/** @brief String representation of KERNEL_FP_TYPE, used in kernel build options. */
#define KERNEL_FP_TYPE_STR "float"

// --- Platform Specific Abstractions and Placeholders ---
#ifndef __linux__
// Windows specific definitions/placeholders
#define PROT_READ 1       /**< Placeholder memory protection flag (read). */
#define PROT_WRITE 2      /**< Placeholder memory protection flag (write). */
#define MAP_SHARED 1      /**< Placeholder memory mapping flag (shared). */
#define MAP_FAILED ((void *) -1) /**< Placeholder for failed memory map. */
/** @brief Placeholder mmap function for non-Linux systems. Returns MAP_FAILED. */
void* mmap(void* addr, size_t length, int prot, int flags, int fd, long offset) { return MAP_FAILED; }
/** @brief Placeholder munmap function for non-Linux systems. Returns -1. */
int munmap(void* addr, size_t length) { return -1; }
/** @brief Placeholder function to read PCI config space (returns 0). */
unsigned int read_pci_config(int gpu_index, int offset) { return 0; }
/** @brief Macro for exporting functions from a DLL on Windows. */
#ifdef __cplusplus
#define DLLEXPORT extern "C" __declspec(dllexport)
#else
#define DLLEXPORT __declspec(dllexport)
#endif
#else
// Linux specific includes and definitions
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
/** @brief Placeholder function to read PCI config space (returns 0). */
unsigned int read_pci_config(int gpu_index, int offset) { return 0; }
/** @brief Macro for exporting functions with default visibility on Linux/GCC. */
#ifdef __cplusplus
#define DLLEXPORT extern "C" __attribute__((visibility("default")))
#else
#define DLLEXPORT __attribute__((visibility("default")))
#endif
#endif

// --- Global Data Type ---
/** @brief Defines the primary floating-point type used on the host side. */
#define FP_TYPE KERNEL_FP_TYPE

#ifndef CC_DRIVER_VERSION
#define CC_DRIVER_VERSION "1.0.0"
#endif

#if defined(_MSC_VER)
#define CC_THREAD_LOCAL __declspec(thread)
#elif defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
#define CC_THREAD_LOCAL _Thread_local
#else
#define CC_THREAD_LOCAL __thread
#endif

#define CC_ERROR_BUFFER_BYTES 512

static CC_THREAD_LOCAL char g_last_error_message[CC_ERROR_BUFFER_BYTES] = "OK";
static CC_THREAD_LOCAL cl_command_queue g_thread_queue = NULL;
static CC_THREAD_LOCAL int g_thread_gpu_index = -1;

// Controls whether kernel launches block until completion (default: blocking enabled).
static int g_force_kernel_finish = 1;

DLLEXPORT void set_kernel_blocking(int blocking_enabled) {
    g_force_kernel_finish = blocking_enabled ? 1 : 0;
    if (g_force_kernel_finish) {
        printf("[C] Kernel-Blocking aktiviert (synchron).\n");
    } else {
        printf("[C] Kernel-Blocking deaktiviert (asynchron).\n");
    }
}

static void cc_set_last_error(const char* fmt, ...) {
    if (!fmt) {
        strncpy(g_last_error_message, "Unknown error", CC_ERROR_BUFFER_BYTES - 1);
        g_last_error_message[CC_ERROR_BUFFER_BYTES - 1] = '\0';
        return;
    }
    va_list args;
    va_start(args, fmt);
    vsnprintf(g_last_error_message, CC_ERROR_BUFFER_BYTES, fmt, args);
    va_end(args);
    g_last_error_message[CC_ERROR_BUFFER_BYTES - 1] = '\0';
}

static inline void cc_clear_last_error(void) {
    strncpy(g_last_error_message, "OK", CC_ERROR_BUFFER_BYTES - 1);
    g_last_error_message[CC_ERROR_BUFFER_BYTES - 1] = '\0';
}


// --- OpenCL Globals ---
/** @brief Handle to the OpenCL context. */
cl_context context = NULL;
/** @brief Handle to the OpenCL command queue. */
cl_command_queue queue = NULL;
/** @brief Handle to the selected OpenCL device ID. */
cl_device_id device_id = NULL;
/** @brief Handle to the selected OpenCL platform ID. */
cl_platform_id platform_id = NULL;
/** @brief Flag indicating if the selected device supports double-precision floating-point (FP64). */
int has_fp64_support = 0;
/**
 * @brief Flag indicating if the device supports necessary atomic operations.
 * Specifically checks for `cl_khr_global_int32_base_atomics`, required by
 * kernels like `proto_segmented_sum_atomic`. Set during initialization.
 */
int has_atomics_support = 0;
/** @brief Tracks if 64-bit atomics are available (for more stable float atomics). */
int has_int64_atomics = 0;

// --- Kernel/Program Variables (Global Handles) ---
cl_program matmul_program = NULL;                 cl_kernel matmul_kernel = NULL;
cl_program matmul_program_fast = NULL;
cl_kernel matmul_kernel_fast = NULL;
cl_program softmax_program = NULL;                cl_kernel softmax_kernel = NULL;
cl_program softmax_program_fast = NULL;
cl_kernel softmax_kernel_fast = NULL;
cl_program gelu_program = NULL;                   cl_kernel gelu_kernel = NULL;
cl_program gelu_program_fast = NULL;
cl_kernel gelu_kernel_fast = NULL;
cl_program add_program = NULL;                    cl_kernel add_kernel = NULL;
cl_program add_program_fast = NULL;
cl_kernel add_kernel_fast = NULL;
cl_program mul_program = NULL;                    cl_kernel mul_kernel = NULL;
cl_program mul_program_fast = NULL;
cl_kernel mul_kernel_fast = NULL;
cl_program layernorm_program = NULL;              cl_kernel layernorm_kernel = NULL;
cl_program layernorm_program_fast = NULL;
cl_kernel layernorm_kernel_fast = NULL;
cl_program transpose_program = NULL;              cl_kernel transpose_kernel = NULL;
cl_program transpose_program_fast = NULL;
cl_kernel transpose_kernel_fast = NULL;
cl_program gelu_backward_program = NULL;          cl_kernel gelu_backward_kernel = NULL;
cl_program gelu_backward_program_fast = NULL;
cl_kernel gelu_backward_kernel_fast = NULL;
cl_program matmul_backward_da_program = NULL;     cl_kernel matmul_backward_da_kernel = NULL;
cl_program matmul_backward_da_program_fast = NULL;
cl_kernel matmul_backward_da_kernel_fast = NULL;
cl_program matmul_backward_db_program = NULL;     cl_kernel matmul_backward_db_kernel = NULL;
cl_program matmul_backward_db_program_fast = NULL;
cl_kernel matmul_backward_db_kernel_fast = NULL;
cl_program layernorm_backward_program = NULL;     cl_kernel layernorm_backward_kernel = NULL;
cl_program layernorm_backward_program_fast = NULL;
cl_kernel layernorm_backward_kernel_fast = NULL;
cl_program adam_program = NULL;                   cl_kernel adam_kernel = NULL;
cl_program adam_program_fast = NULL;
cl_kernel adam_kernel_fast = NULL;
cl_program softmax_backward_program = NULL;       cl_kernel softmax_backward_kernel = NULL;
cl_program softmax_backward_program_fast = NULL;
cl_kernel softmax_backward_kernel_fast = NULL;
cl_program mul_backward_program = NULL;           cl_kernel mul_backward_kernel = NULL;
cl_program mul_backward_program_fast = NULL;
cl_kernel mul_backward_kernel_fast = NULL;
cl_program transpose_backward_program = NULL;     cl_kernel transpose_backward_kernel = NULL;
cl_program transpose_backward_program_fast = NULL;
cl_kernel transpose_backward_kernel_fast = NULL;
cl_program embedding_lookup_program = NULL;       cl_kernel embedding_lookup_kernel = NULL;
cl_program embedding_lookup_program_fast = NULL;
cl_kernel embedding_lookup_kernel_fast = NULL;
cl_program reduce_sum_program = NULL;             cl_kernel reduce_sum_kernel = NULL;
cl_program reduce_sum_program_fast = NULL;
cl_kernel reduce_sum_kernel_fast = NULL;
cl_program broadcast_add_program = NULL;          cl_kernel broadcast_add_kernel = NULL;
cl_program broadcast_add_program_fast = NULL;
cl_kernel broadcast_add_kernel_fast = NULL;
cl_program transpose_batched_program = NULL;      cl_kernel transpose_batched_kernel = NULL;
cl_program transpose_batched_program_fast = NULL;
cl_kernel transpose_batched_kernel_fast = NULL;
cl_program transpose_12_batched_program = NULL;   cl_kernel transpose_12_batched_kernel = NULL;
cl_program transpose_12_batched_program_fast = NULL;
cl_kernel transpose_12_batched_kernel_fast = NULL;
cl_program matmul_batched_program = NULL;         cl_kernel matmul_batched_kernel = NULL;
cl_program matmul_batched_program_fast = NULL;
cl_kernel matmul_batched_kernel_fast = NULL;

// --- GPU Slot Manager (Multi-Device Preparation) ---
typedef struct GpuSlot {
    cl_platform_id    platform;
    cl_device_id      device;
    cl_context        context;
    cl_command_queue  queue;
    cl_command_queue  transfer_queue;
    cl_program        program;
    cl_mem            pinned_amp_buffer;
    cl_float2*        pinned_amp_host;
    size_t            pinned_amp_bytes;
    cl_int            initialized;
    cl_int            in_error;
    cl_int            owns_objects;
    cl_int            out_of_order_enabled;
} GpuSlot;

#define CC_MAX_DEVICES 8
#define CC_PINNED_STAGING_MIN_BYTES 4096

static GpuSlot g_gpu_slots[CC_MAX_DEVICES];
static int     g_slot_count_discovered = -1;

#ifdef _WIN32
static CRITICAL_SECTION g_slots_lock;
static int g_slots_lock_inited = 0;
static void cc_lock_init_once(void) {
    if (!g_slots_lock_inited) {
        InitializeCriticalSection(&g_slots_lock);
        g_slots_lock_inited = 1;
    }
}
#define CC_LOCK()   EnterCriticalSection(&g_slots_lock)
#define CC_UNLOCK() LeaveCriticalSection(&g_slots_lock)
#else
static pthread_mutex_t g_slots_lock = PTHREAD_MUTEX_INITIALIZER;
static void cc_lock_init_once(void) { (void)g_slots_lock; }
#define CC_LOCK()   pthread_mutex_lock(&g_slots_lock)
#define CC_UNLOCK() pthread_mutex_unlock(&g_slots_lock)
#endif

static inline int is_line_comment(const char* text) {
    return text && (text[0] == '#' || (text[0] == '/' && text[1] == '/'));
}

static char* trim_whitespace(char* str) {
    if (!str) { return str; }
    while (*str && isspace((unsigned char)*str)) { ++str; }
    char* end = str + strlen(str);
    while (end > str) {
        if (!isspace((unsigned char)end[-1])) { break; }
        --end;
    }
    *end = '\0';
    return str;
}

static double cc_now_ms(void) {
#ifdef _WIN32
    LARGE_INTEGER freq, counter;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart * 1000.0 / (double)freq.QuadPart;
#else
    struct timespec ts;
#ifdef CLOCK_MONOTONIC
    clock_gettime(CLOCK_MONOTONIC, &ts);
#else
    clock_gettime(CLOCK_REALTIME, &ts);
#endif
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
#endif
}

static int  cc_discover_devices_once(void);
static int  cc_ensure_slot_initialized(int gpu_index);
static GpuSlot* cc_get_slot(int gpu_index);
static void cc_mark_slot_initialized(int gpu_index, cl_context ctx, cl_command_queue q, cl_program program);
static void cc_reset_slot(GpuSlot* slot);
static void cc_release_all_slots(void);
static cl_command_queue cc_get_slot_queue(int gpu_index, int prefer_transfer, GpuSlot** out_slot);
cl_program matmul_batched_backward_da_program = NULL; cl_kernel matmul_batched_backward_da_kernel = NULL;
cl_program matmul_batched_backward_da_program_fast = NULL;
cl_kernel matmul_batched_backward_da_kernel_fast = NULL;
cl_program matmul_batched_backward_db_program = NULL; cl_kernel matmul_batched_backward_db_kernel = NULL;
cl_program matmul_batched_backward_db_program_fast = NULL;
cl_kernel matmul_batched_backward_db_kernel_fast = NULL;
cl_program log_softmax_program = NULL;            cl_kernel log_softmax_kernel = NULL;
cl_program log_softmax_program_fast = NULL;
cl_kernel log_softmax_kernel_fast = NULL;
cl_program cross_entropy_program = NULL;          cl_kernel cross_entropy_kernel = NULL;
cl_program cross_entropy_program_fast = NULL;
cl_kernel cross_entropy_kernel_fast = NULL;
cl_program add_broadcast_pe_program = NULL;       cl_kernel add_broadcast_pe_kernel = NULL;
cl_program add_broadcast_pe_program_fast = NULL;
cl_kernel add_broadcast_pe_kernel_fast = NULL;
cl_program threshold_spike_program = NULL;        cl_kernel threshold_spike_kernel = NULL;
cl_program threshold_spike_program_fast = NULL;
cl_kernel threshold_spike_kernel_fast = NULL;
cl_program add_bias_mn_program = NULL;            cl_kernel add_bias_mn_kernel = NULL;
cl_program add_bias_mn_program_fast = NULL;
cl_kernel add_bias_mn_kernel_fast = NULL;
cl_program dynamic_token_assign_program = NULL;   cl_kernel dynamic_token_assign_kernel = NULL;
cl_program dynamic_token_assign_program_fast = NULL;
cl_kernel dynamic_token_assign_kernel_fast = NULL;
cl_program pairwise_similarity_program = NULL;    cl_kernel pairwise_similarity_kernel = NULL;
cl_program pairwise_similarity_program_fast = NULL;
cl_kernel pairwise_similarity_kernel_fast = NULL;
cl_program fused_diffusion_program = NULL;        cl_kernel fused_diffusion_kernel = NULL;
cl_program fused_diffusion_program_fast = NULL;
cl_kernel fused_diffusion_kernel_fast = NULL;
cl_program conv2d_forward_program = NULL;         cl_kernel conv2d_forward_kernel = NULL;
cl_program conv2d_forward_program_fast = NULL;
cl_kernel conv2d_forward_kernel_fast = NULL;
cl_program conv2d_backward_input_program = NULL;  cl_kernel conv2d_backward_input_kernel = NULL;
cl_program conv2d_backward_input_program_fast = NULL;
cl_kernel conv2d_backward_input_kernel_fast = NULL;
cl_program conv2d_backward_weight_program = NULL; cl_kernel conv2d_backward_weight_kernel = NULL;
cl_program conv2d_backward_weight_program_fast = NULL;
cl_kernel conv2d_backward_weight_kernel_fast = NULL;
cl_program conv2d_bias_grad_program = NULL;       cl_kernel conv2d_bias_grad_kernel = NULL;
cl_program conv2d_bias_grad_program_fast = NULL;
cl_kernel conv2d_bias_grad_kernel_fast = NULL;
cl_program patch_permute_program = NULL;          cl_kernel patch_permute_kernel = NULL;
cl_program patch_permute_program_fast = NULL;
cl_kernel patch_permute_kernel_fast = NULL;
cl_program patch_permute_backward_program = NULL; cl_kernel patch_permute_backward_kernel = NULL;
cl_program patch_permute_backward_program_fast = NULL;
cl_kernel patch_permute_backward_kernel_fast = NULL;
static unsigned int g_rng_seed = 0; // Einfacher Zähler für den RNG-Seed
cl_program izhikevich_program = NULL;           cl_kernel izhikevich_kernel = NULL;
cl_program izhikevich_program_fast = NULL;      cl_kernel izhikevich_kernel_fast = NULL;
cl_program stdp_update_program = NULL;          cl_kernel stdp_update_kernel = NULL;
cl_program stdp_update_program_fast = NULL;     cl_kernel stdp_update_kernel_fast = NULL;
cl_program stdp_trace_program = NULL;           cl_kernel stdp_trace_kernel = NULL;
cl_program stdp_trace_program_fast = NULL;      cl_kernel stdp_trace_kernel_fast = NULL;
cl_program lbm_program = NULL;                  cl_kernel lbm_kernel = NULL;
cl_program lbm_program_fast = NULL;             cl_kernel lbm_kernel_fast = NULL;
cl_program nbody_forces_program = NULL;         cl_kernel nbody_forces_kernel = NULL;
cl_program nbody_forces_program_fast = NULL;    cl_kernel nbody_forces_kernel_fast = NULL;
cl_program nbody_integrate_program = NULL;      cl_kernel nbody_integrate_kernel = NULL;
cl_program nbody_integrate_program_fast = NULL; cl_kernel nbody_integrate_kernel_fast = NULL;
cl_program ising_program = NULL;                cl_kernel ising_kernel = NULL;
cl_program ising_program_fast = NULL;           cl_kernel ising_kernel_fast = NULL;
cl_program hebbian_update_local_reduce_program = NULL; cl_kernel hebbian_update_local_reduce_kernel = NULL;
cl_program hebbian_update_local_reduce_program_fast = NULL;
cl_kernel hebbian_update_local_reduce_kernel_fast = NULL;
cl_program embedding_backward_calc_delta_local_program = NULL; cl_kernel embedding_backward_calc_delta_local_kernel = NULL;
cl_program embedding_backward_calc_delta_local_program_fast = NULL;
cl_kernel embedding_backward_calc_delta_local_kernel_fast = NULL;
// Prototype Update Kernels
cl_program proto_segmented_sum_program = NULL;   cl_kernel proto_segmented_sum_kernel = NULL;
cl_program proto_segmented_sum_program_fast = NULL;
cl_kernel proto_segmented_sum_kernel_fast = NULL;
cl_program proto_update_step_program = NULL;     cl_kernel proto_update_step_kernel = NULL;
cl_program proto_update_step_program_fast = NULL;
cl_kernel proto_update_step_kernel_fast = NULL;
// Loss Shaping Kernels (Keep both for potential compatibility)
cl_program shape_loss_reward_penalty_program = NULL; cl_kernel shape_loss_reward_penalty_kernel = NULL;
cl_program shape_loss_reward_penalty_program_fast = NULL;
cl_kernel shape_loss_reward_penalty_kernel_fast = NULL;
cl_program shape_loss_reward_penalty_list_program = NULL; cl_kernel shape_loss_reward_penalty_list_kernel = NULL;
cl_program shape_loss_reward_penalty_list_program_fast = NULL;
cl_kernel shape_loss_reward_penalty_list_kernel_fast = NULL; // NEU
// SubQG Simulation Kernel
cl_program subqg_simulation_program = NULL;       cl_kernel subqg_simulation_kernel = NULL;
cl_program subqg_simulation_program_fast = NULL;
cl_kernel subqg_simulation_kernel_fast = NULL;
cl_program subqg_agent_program = NULL;            cl_kernel subqg_agent_kernel = NULL;
cl_program sqse_program = NULL;                   cl_kernel sqse_encrypt_kernel = NULL;
cl_kernel sqse_decrypt_kernel = NULL;

// Quantum Algorithm Kernels
cl_program quantum_program = NULL;
cl_kernel quantum_single_qubit_kernel = NULL;
cl_kernel quantum_controlled_phase_kernel = NULL;
cl_kernel quantum_controlled_not_kernel = NULL;
cl_kernel quantum_phase_oracle_kernel = NULL;
cl_kernel quantum_phase_zero_kernel = NULL;
cl_kernel quantum_modexp_kernel = NULL;
cl_kernel quantum_swap_kernel = NULL;
cl_kernel quantum_probability_kernel = NULL;
cl_kernel quantum_expectation_pauli_z_kernel = NULL;
cl_kernel quantum_apply_gate_kernel = NULL;
cl_kernel quantum_vqe_gradient_kernel = NULL;
cl_kernel qualia_resonator_kernel = NULL;
cl_kernel intuition_precognition_kernel = NULL;
cl_kernel context_resonance_kernel = NULL;
cl_kernel dream_state_generator_kernel = NULL;
cl_kernel transformation_planner_kernel = NULL;
cl_kernel system_narrative_kernel = NULL;
cl_kernel symbolic_abstraction_kernel = NULL;

static int cc_env_quantum_disabled(void) {
    const char* value = getenv("CC_DISABLE_QUANTUM");
    if (!value) {
        return 0;
    }

    while (*value && isspace((unsigned char)*value)) {
        value++;
    }
    if (*value == '\0') {
        return 0;
    }

    if (*value == '0') {
        return 0;
    }

    if (cc_strncasecmp(value, "false", 5) == 0) {
        return 0;
    }

    if (cc_strncasecmp(value, "off", 3) == 0) {
        return 0;
    }

    return 1;
}

static int g_quantum_enabled = 1;
static int g_quantum_disabled_warned = 0;

cl_program mycel_program = NULL;
cl_kernel mycel_reinforce_kernel = NULL;
cl_kernel mycel_diffuse_kernel = NULL;
cl_kernel mycel_nutrient_kernel = NULL;

cl_program render_program = NULL;
cl_kernel render_kernel_img = NULL;
cl_kernel render_kernel_buf = NULL;
cl_kernel render_debug_kernel = NULL;

static int g_force_debug_render = -1;
static int g_debug_smoke_test_done = 0;

static void mycel_release_gpu_buffers(MycelState* state) {
    if (!state) {
        return;
    }
    if (state->pheromone_buf) { clReleaseMemObject(state->pheromone_buf); state->pheromone_buf = NULL; }
    if (state->neigh_idx_buf) { clReleaseMemObject(state->neigh_idx_buf); state->neigh_idx_buf = NULL; }
    if (state->decay_buf) { clReleaseMemObject(state->decay_buf); state->decay_buf = NULL; }
    if (state->diffu_buf) { clReleaseMemObject(state->diffu_buf); state->diffu_buf = NULL; }
    if (state->nutrient_buf) { clReleaseMemObject(state->nutrient_buf); state->nutrient_buf = NULL; }
    if (state->mood_buf) { clReleaseMemObject(state->mood_buf); state->mood_buf = NULL; }
    if (state->alive_buf) { clReleaseMemObject(state->alive_buf); state->alive_buf = NULL; }
    if (state->reinforce_gain_buf) { clReleaseMemObject(state->reinforce_gain_buf); state->reinforce_gain_buf = NULL; }
}

static int mycel_upload_buffer(cl_mem buffer, const void* data, size_t bytes, const char* name) {
    if (!buffer || bytes == 0) {
        return 1;
    }
    if (!queue) {
        fprintf(stderr, "[C] mycel_upload_buffer: Command queue unavailable for %s.\n", name ? name : "buffer");
        return 0;
    }
    cl_int err = clEnqueueWriteBuffer(queue, buffer, CL_TRUE, 0, bytes, data, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] mycel_upload_buffer: Failed to upload %s: %s (%d).\n", name ? name : "buffer", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int mycel_download_buffer(cl_mem buffer, void* data, size_t bytes, const char* name) {
    if (!buffer || bytes == 0) {
        return 1;
    }
    if (!queue) {
        fprintf(stderr, "[C] mycel_download_buffer: Command queue unavailable for %s.\n", name ? name : "buffer");
        return 0;
    }
    cl_int err = clEnqueueReadBuffer(queue, buffer, CL_TRUE, 0, bytes, data, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] mycel_download_buffer: Failed to download %s: %s (%d).\n", name ? name : "buffer", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int mycel_float_cmp(const void* a, const void* b) {
    float fa = *(const float*)a;
    float fb = *(const float*)b;
    if (fa < fb) { return -1; }
    if (fa > fb) { return 1; }
    return 0;
}

static float mycel_estimate_pheromone_percentile(const MycelState* state,
                                                 size_t width, size_t height,
                                                 float percentile) {
    if (!state || !state->pheromone || state->C <= 0 || state->K <= 0) {
        return 0.0f;
    }
    size_t agent_cap = (size_t)state->T_cap;
    if (agent_cap == 0) {
        return 0.0f;
    }

    size_t agent_count = width * height;
    if (agent_count > agent_cap) {
        agent_count = agent_cap;
    }
    if (agent_count == 0) {
        return 0.0f;
    }

    int channel_count = state->C < 3 ? state->C : 3;
    if (channel_count <= 0) {
        return 0.0f;
    }

    const size_t max_samples = 16384;
    size_t step = agent_count / max_samples;
    if (step == 0) {
        step = 1;
    }
    size_t sample_count = (agent_count + step - 1) / step;
    size_t total_samples = sample_count * (size_t)channel_count;
    float* samples = (float*)malloc(total_samples * sizeof(float));
    if (!samples) {
        return 0.0f;
    }

    size_t stride = (size_t)state->K * (size_t)state->C;
    size_t sample_index = 0;
    for (size_t agent = 0; agent < agent_count; agent += step) {
        size_t base = agent * stride;
        for (int c = 0; c < channel_count; ++c) {
            samples[sample_index++] = fabsf(state->pheromone[base + (size_t)c]);
        }
    }

    if (sample_index == 0) {
        free(samples);
        return 0.0f;
    }

    qsort(samples, sample_index, sizeof(float), mycel_float_cmp);

    float pct = percentile;
    if (pct < 0.0f) {
        pct = 0.0f;
    }
    if (pct > 100.0f) {
        pct = 100.0f;
    }
    double fraction = sample_index > 1 ? (double)pct / 100.0 : 1.0;
    size_t idx;
    if (fraction <= 0.0) {
        idx = 0;
    } else if (fraction >= 1.0) {
        idx = sample_index - 1;
    } else {
        idx = (size_t)floor(fraction * (double)(sample_index - 1));
        if (idx >= sample_index) {
            idx = sample_index - 1;
        }
    }

    float clip = samples[idx];
    free(samples);
    return clip;
}

static void mycel_free_state(MycelState* state) {
    if (!state) {
        return;
    }
    mycel_release_gpu_buffers(state);
    free(state->pheromone);
    free(state->neigh_idx);
    free(state->decay);
    free(state->diffu);
    free(state->nutrient);
    free(state->mood);
    free(state->colony_id);
    free(state->alive);
    free(state->potential);
    free(state->subqg_field);
    free(state->free_list);
    free(state->reinforce_gain);
    free(state->kappa_mood);
    memset(state, 0, sizeof(MycelState));
}

static int mycel_ensure_gpu_buffers(MycelState* state) {
    if (!state) {
        return 0;
    }
    if (state->pheromone_buf) {
        return 1;
    }
    if (!context || !queue) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: OpenCL context or queue unavailable.\n");
        return 0;
    }
    cl_int err;
    size_t edge_count = (size_t)state->T_cap * (size_t)state->K;
    size_t pher_bytes = edge_count * (size_t)state->C * sizeof(float);
    size_t neigh_bytes = edge_count * sizeof(int);
    size_t decay_bytes = edge_count * sizeof(float);
    size_t diffu_bytes = edge_count * sizeof(float);
    size_t nutrient_bytes = (size_t)state->T_cap * sizeof(float);
    size_t mood_bytes = (size_t)state->T_cap * (size_t)state->C * sizeof(float);
    size_t alive_bytes = (size_t)state->T_cap * sizeof(uint8_t);
    size_t gain_bytes = (size_t)state->C * sizeof(float);

    state->pheromone_buf = (pher_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, pher_bytes, NULL, &err) : NULL;
    if (pher_bytes > 0 && (!state->pheromone_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate pheromone buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->neigh_idx_buf = (neigh_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, neigh_bytes, NULL, &err) : NULL;
    if (neigh_bytes > 0 && (!state->neigh_idx_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate neighbor buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->decay_buf = (decay_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, decay_bytes, NULL, &err) : NULL;
    if (decay_bytes > 0 && (!state->decay_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate decay buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->diffu_buf = (diffu_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, diffu_bytes, NULL, &err) : NULL;
    if (diffu_bytes > 0 && (!state->diffu_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate diffusion buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->nutrient_buf = (nutrient_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, nutrient_bytes, NULL, &err) : NULL;
    if (nutrient_bytes > 0 && (!state->nutrient_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate nutrient buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->mood_buf = (mood_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, mood_bytes, NULL, &err) : NULL;
    if (mood_bytes > 0 && (!state->mood_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate mood buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->alive_buf = (alive_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, alive_bytes, NULL, &err) : NULL;
    if (alive_bytes > 0 && (!state->alive_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate alive buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }
    state->reinforce_gain_buf = (gain_bytes > 0) ? clCreateBuffer(context, CL_MEM_READ_WRITE, gain_bytes, NULL, &err) : NULL;
    if (gain_bytes > 0 && (!state->reinforce_gain_buf || err != CL_SUCCESS)) {
        fprintf(stderr, "[C] mycel_ensure_gpu_buffers: Failed to allocate reinforce buffer: %s (%d).\n", clGetErrorString(err), err);
        mycel_release_gpu_buffers(state);
        return 0;
    }

    if (!mycel_upload_buffer(state->pheromone_buf, state->pheromone, pher_bytes, "pheromone") ||
        !mycel_upload_buffer(state->neigh_idx_buf, state->neigh_idx, neigh_bytes, "neigh_idx") ||
        !mycel_upload_buffer(state->decay_buf, state->decay, decay_bytes, "decay") ||
        !mycel_upload_buffer(state->diffu_buf, state->diffu, diffu_bytes, "diffu") ||
        !mycel_upload_buffer(state->nutrient_buf, state->nutrient, nutrient_bytes, "nutrient") ||
        !mycel_upload_buffer(state->mood_buf, state->mood, mood_bytes, "mood") ||
        !mycel_upload_buffer(state->alive_buf, state->alive, alive_bytes, "alive") ||
        !mycel_upload_buffer(state->reinforce_gain_buf, state->reinforce_gain, gain_bytes, "reinforce_gain")) {
        mycel_release_gpu_buffers(state);
        return 0;
    }

    return 1;
}

static int mycel_upload_all_state(MycelState* state) {
    if (!mycel_ensure_gpu_buffers(state)) {
        return 0;
    }
    size_t edge_count = (size_t)state->T_cap * (size_t)state->K;
    size_t pher_bytes = edge_count * (size_t)state->C * sizeof(float);
    size_t neigh_bytes = edge_count * sizeof(int);
    size_t decay_bytes = edge_count * sizeof(float);
    size_t diffu_bytes = edge_count * sizeof(float);
    size_t nutrient_bytes = (size_t)state->T_cap * sizeof(float);
    size_t mood_bytes = (size_t)state->T_cap * (size_t)state->C * sizeof(float);
    size_t alive_bytes = (size_t)state->T_cap * sizeof(uint8_t);
    size_t gain_bytes = (size_t)state->C * sizeof(float);

    return mycel_upload_buffer(state->pheromone_buf, state->pheromone, pher_bytes, "pheromone") &&
           mycel_upload_buffer(state->neigh_idx_buf, state->neigh_idx, neigh_bytes, "neigh_idx") &&
           mycel_upload_buffer(state->decay_buf, state->decay, decay_bytes, "decay") &&
           mycel_upload_buffer(state->diffu_buf, state->diffu, diffu_bytes, "diffu") &&
           mycel_upload_buffer(state->nutrient_buf, state->nutrient, nutrient_bytes, "nutrient") &&
           mycel_upload_buffer(state->mood_buf, state->mood, mood_bytes, "mood") &&
           mycel_upload_buffer(state->alive_buf, state->alive, alive_bytes, "alive") &&
           mycel_upload_buffer(state->reinforce_gain_buf, state->reinforce_gain, gain_bytes, "reinforce_gain");
}

static size_t mycel_edge_count(const MycelState* state) {
    return (size_t)state->T_cap * (size_t)state->K;
}

static size_t mycel_pheromone_count(const MycelState* state) {
    return (size_t)state->T_cap * (size_t)state->K * (size_t)state->C;
}

static int mycel_clamp_index(const MycelState* state, int idx) {
    if (idx < 0 || idx >= state->T_cap) {
        return -1;
    }
    return idx;
}

static float mycel_random_normal(void) {
    // Basic Box-Muller using rand(); fall back to uniform noise if needed.
    float u1 = (float)rand() / (float)RAND_MAX;
    float u2 = (float)rand() / (float)RAND_MAX;
    if (u1 < 1e-6f) {
        u1 = 1e-6f;
    }
    float mag = sqrtf(-2.0f * logf(u1));
    return mag * cosf(2.0f * (float)M_PI * u2);
}

static bool mycel_check_initialized(const MycelState* state) {
    return state->initialized;
}

static int mycel_pop_free(MycelState* state) {
    if (state->free_head <= 0) {
        return -1;
    }
    state->free_head -= 1;
    return state->free_list[state->free_head];
}

static void mycel_push_free(MycelState* state, int idx) {
    if (!state->free_list || idx < 0 || idx >= state->T_cap) {
        return;
    }
    state->free_list[state->free_head] = idx;
    state->free_head += 1;
}

static void mycel_recompute_active_count(MycelState* state) {
    int max_idx = -1;
    for (int i = 0; i < state->T_cap; ++i) {
        if (state->alive && state->alive[i]) {
            if (i > max_idx) {
                max_idx = i;
            }
        }
    }
    state->T_act = max_idx + 1;
}

static int mycel_initialize(MycelState* state, int T_cap, int C, int K) {
    if (T_cap <= 0 || C <= 0 || K <= 0) {
        return 0;
    }
    if (!context || !queue) {
        fprintf(stderr, "[C] mycel_initialize: OpenCL context not initialized.\n");
        return 0;
    }
    mycel_free_state(state);

    size_t edge_count = (size_t)T_cap * (size_t)K;
    size_t pheromone_count = edge_count * (size_t)C;

    state->T_cap = T_cap;
    state->C = C;
    state->K = K;
    state->T_act = 0;

    state->pheromone = (float*)calloc(pheromone_count, sizeof(float));
    state->neigh_idx = (int*)malloc(edge_count * sizeof(int));
    state->decay = (float*)malloc(edge_count * sizeof(float));
    state->diffu = (float*)malloc(edge_count * sizeof(float));
    state->nutrient = (float*)calloc(T_cap, sizeof(float));
    state->mood = (float*)calloc((size_t)T_cap * (size_t)C, sizeof(float));
    state->colony_id = (uint8_t*)calloc(T_cap, sizeof(uint8_t));
    state->alive = (uint8_t*)calloc(T_cap, sizeof(uint8_t));
    state->potential = (float*)calloc(T_cap, sizeof(float));
    state->subqg_field = (float*)calloc(T_cap, sizeof(float));
    state->free_list = (int*)malloc(T_cap * sizeof(int));
    state->reinforce_gain = (float*)calloc(C, sizeof(float));
    state->kappa_mood = (float*)calloc(C, sizeof(float));

    if (!state->pheromone || !state->neigh_idx || !state->decay || !state->diffu ||
        !state->nutrient || !state->mood || !state->colony_id || !state->alive ||
        !state->potential || !state->subqg_field || !state->free_list ||
        !state->reinforce_gain || !state->kappa_mood) {
        mycel_free_state(state);
        return 0;
    }

    for (size_t i = 0; i < edge_count; ++i) {
        state->neigh_idx[i] = -1;
        state->decay[i] = 0.0f;
        state->diffu[i] = 0.0f;
    }

    for (int i = 0; i < T_cap; ++i) {
        state->free_list[i] = i;
    }
    state->free_head = T_cap;
    state->repro_thr_nutrient = 0.0f;
    state->repro_thr_activity = 0.0f;
    state->repro_mut_sigma = 0.0f;
    state->decay_default = 0.0f;
    state->diffu_default = 0.0f;
    state->nutrient_recovery = 0.01f;
    state->kappa_nutrient = 0.0f;
    if (!mycel_upload_all_state(state)) {
        mycel_free_state(state);
        return 0;
    }
    state->initialized = true;
    return 1;
}

typedef struct {
    char name[64];
    float duration_ms;
    float error;
    float variance;
} KernelMetricsSample;

typedef struct {
    char name[8];
    cl_uint arity;
    cl_uint control;
    cl_uint target;
    cl_uint control2;
    float params[4];
    cl_float2 matrix[8][8];
} QuantumGate;

// --- SubQG Simulation Buffers / State ---
static cl_mem subqg_energy_buffer = NULL;
static cl_mem subqg_phase_buffer = NULL;
static cl_mem subqg_interference_buffer = NULL;
static cl_mem subqg_node_flag_buffer = NULL;
static cl_mem subqg_spin_buffer = NULL;
static cl_mem subqg_topology_buffer = NULL;
static cl_mem subqg_rng_energy_buffer = NULL;
static cl_mem subqg_rng_phase_buffer = NULL;
static cl_mem subqg_rng_spin_buffer = NULL;
static cl_mem subqg_field_map_buffer = NULL;
static cl_mem subqg_agent_buffer = NULL;
static size_t subqg_agent_buffer_bytes = 0;
static float subqg_noise_level = 0.0f;
static float subqg_threshold = 0.0f;
static int subqg_cell_count = 0;
static int subqg_deterministic_mode = 0;
static uint64_t subqg_rng_seed = 0;
static uint64_t subqg_rng_state = 0;
static int subqg_state_initialized = 0;
static int subqg_field_map_elements = 0;
static int subqg_width = 0;
static int subqg_height = 0;

#define SUBQG_SIM_ARG_FIELD_MAP 13
#define SUBQG_SIM_ARG_WRITE_FLAG 14

DLLEXPORT int subqg_initialize_state_batched(int gpu_index, int cell_count,
                                             const float* initial_energy, const float* initial_phase,
                                             float noise_level, float threshold);

DLLEXPORT int subqg_debug_read_field(float* out_host, int max_len);

static int ensure_subqg_state(int width, int height) {
    if (width <= 0 || height <= 0) {
        fprintf(stderr, "[C] ensure_subqg_state: invalid grid %dx%d\n", width, height);
        return 0;
    }

    if (subqg_field_map_buffer && subqg_width == width && subqg_height == height) {
        return 1;
    }

    const int cells = width * height;
    const float noise_level = subqg_noise_level;
    const float threshold = subqg_threshold;

    if (!subqg_initialize_state_batched(/*gpu_index*/0, cells, NULL, NULL,
                                        noise_level, threshold)) {
        fprintf(stderr, "[C] ensure_subqg_state: subqg_initialize_state_batched(%d) failed.\n", cells);
        return 0;
    }

    if (!subqg_field_map_buffer) {
        fprintf(stderr, "[C] ensure_subqg_state: field map buffer missing after init.\n");
        return 0;
    }

    subqg_width = width;
    subqg_height = height;
    subqg_cell_count = cells;
    subqg_field_map_elements = cells;

    cl_int err_set = CL_SUCCESS;
    const cl_int write_flag = 1;
    if (subqg_simulation_kernel) {
        err_set |= clSetKernelArg(subqg_simulation_kernel, SUBQG_SIM_ARG_FIELD_MAP,
                                  sizeof(cl_mem), &subqg_field_map_buffer);
        err_set |= clSetKernelArg(subqg_simulation_kernel, SUBQG_SIM_ARG_WRITE_FLAG,
                                  sizeof(cl_int), &write_flag);
    }
    if (subqg_simulation_kernel_fast) {
        err_set |= clSetKernelArg(subqg_simulation_kernel_fast, SUBQG_SIM_ARG_FIELD_MAP,
                                  sizeof(cl_mem), &subqg_field_map_buffer);
        err_set |= clSetKernelArg(subqg_simulation_kernel_fast, SUBQG_SIM_ARG_WRITE_FLAG,
                                  sizeof(cl_int), &write_flag);
    }

    if (err_set != CL_SUCCESS) {
        fprintf(stderr, "[C] ensure_subqg_state: Failed to bind field buffer to kernels: %s (%d)\n",
                clGetErrorString(err_set), err_set);
        return 0;
    }

    subqg_state_initialized = 1;
    return 1;
}

// --- Quantum Simulation Scratch Buffers ---
static cl_mem quantum_temp_state_buffer = NULL;
static size_t quantum_temp_state_bytes = 0;
static cl_mem quantum_probability_buffer = NULL;
static size_t quantum_probability_bytes = 0;
static cl_mem quantum_gate_sequence_buffer = NULL;
static size_t quantum_gate_sequence_bytes = 0;
static QuantumGate* quantum_gate_host_sequence = NULL;
static size_t quantum_gate_host_count = 0;
static int quantum_gate_sequence_last_qubits = 0;

static void quantum_gate_init(QuantumGate* gate, const char* name) {
    if (!gate) { return; }
    memset(gate, 0, sizeof(*gate));
    if (name) {
        strncpy(gate->name, name, sizeof(gate->name) - 1);
    }
}

static int quantum_parse_qubit_index(const char* token, int* out_index) {
    if (!token || !out_index) { return 0; }
    const char* start = strchr(token, '[');
    const char* end = strchr(token, ']');
    if (!start || !end || end <= start + 1) { return 0; }
    char buffer[16];
    size_t len = (size_t)(end - start - 1);
    if (len >= sizeof(buffer)) { return 0; }
    memcpy(buffer, start + 1, len);
    buffer[len] = '\0';
    char* endptr = NULL;
    long value = strtol(buffer, &endptr, 10);
    if (endptr == buffer || value < 0) { return 0; }
    *out_index = (int)value;
    return 1;
}

static int quantum_parse_float(const char* text, float* out_value) {
    if (!text || !out_value) { return 0; }
    while (*text && isspace((unsigned char)*text)) { ++text; }
    int sign = 1;
    if (*text == '+') { ++text; }
    else if (*text == '-') { sign = -1; ++text; }

    if (cc_strncasecmp(text, "PI", 2) == 0) {
        double multiplier = 1.0;
        double divisor = 1.0;
        const char* after = text + 2;
        if (*after == '*') {
            char* endptr = NULL;
            multiplier = strtod(after + 1, &endptr);
            after = endptr;
        }
        if (*after == '/' || strchr(after, '/')) {
            const char* slash = strchr(after, '/');
            if (slash) {
                char* endptr = NULL;
                divisor = strtod(slash + 1, &endptr);
                if (divisor == 0.0) { return 0; }
            }
        }
        *out_value = (float)(sign * M_PI * multiplier / divisor);
        return 1;
    }

    char* endptr = NULL;
    double val = strtod(text, &endptr);
    if (endptr == text) { return 0; }
    *out_value = (float)(sign * val);
    return 1;
}

static int quantum_parse_three_floats(const char* text, float* out_vals) {
    if (!text || !out_vals) { return 0; }
    float a = 0.0f, b = 0.0f, c = 0.0f;
    int consumed = sscanf(text, "(%f,%f,%f)", &a, &b, &c);
    if (consumed != 3) { return 0; }
    out_vals[0] = a;
    out_vals[1] = b;
    out_vals[2] = c;
    return 1;
}

static int quantum_append_gate(QuantumGate* out_gates, int max_gates, int* gate_count, const QuantumGate* gate) {
    if (!out_gates || !gate_count || !gate) { return 0; }
    if (*gate_count >= max_gates) { return 0; }
    out_gates[*gate_count] = *gate;
    (*gate_count)++;
    return 1;
}

static KernelMetricsSample g_last_metrics = {"", 0.0f, 0.0f, 0.0f};
static float* g_measurement_error_target = NULL;
static float* g_measurement_variance_target = NULL;

static inline float cc_log_sum_exp_pair(float a, float b) {
    if (a == -INFINITY) { return b; }
    if (b == -INFINITY) { return a; }
    float max_val = (a > b) ? a : b;
    return max_val + logf(expf(a - max_val) + expf(b - max_val));
}

static inline float cc_log_sum_exp_three(float a, float b, float c) {
    float max_val = a;
    if (b > max_val) { max_val = b; }
    if (c > max_val) { max_val = c; }
    if (max_val == -INFINITY) { return -INFINITY; }
    float sum = 0.0f;
    if (a != -INFINITY) { sum += expf(a - max_val); }
    if (b != -INFINITY) { sum += expf(b - max_val); }
    if (c != -INFINITY) { sum += expf(c - max_val); }
    return max_val + logf(sum);
}


/**
 * @brief Enumeration of available GPU commands that can be submitted via the driver.
 * Each enum value corresponds to a specific OpenCL kernel or operation.
 */
typedef enum {
    COMMAND_MATRIX_MULTIPLY = 1,                /**< Standard matrix multiply (C = A @ B). */
    COMMAND_SOFTMAX_ROWWISE = 2,                /**< Row-wise numerically stable softmax. */
    COMMAND_GELU_ELEMENTWISE = 3,               /**< Element-wise GELU activation. */
    COMMAND_ADD_ELEMENTWISE = 4,                /**< Element-wise addition (C = A + B). Also used for Embedding Bwd Pass 2. */
    COMMAND_MUL_ELEMENTWISE = 5,                /**< Element-wise multiplication (C = A * B). */
    COMMAND_LAYER_NORM = 6,                     /**< Layer normalization (row-wise, no affine params). */
    COMMAND_CLONE = 7,                          /**< Simple buffer copy (clEnqueueCopyBuffer). */
    COMMAND_TRANSPOSE = 8,                      /**< Basic 2D matrix transpose. */
    COMMAND_GELU_BACKWARD_ELEMENTWISE = 9,      /**< Element-wise backward pass for GELU. */
    COMMAND_MATMUL_BACKWARD_DA = 10,            /**< Backward pass for matmul, calculating gradient dA. */
    COMMAND_MATMUL_BACKWARD_DB = 11,            /**< Backward pass for matmul, calculating gradient dB. */
    COMMAND_LAYER_NORM_BACKWARD = 12,           /**< Backward pass for layer normalization. */
    COMMAND_ADAM_UPDATE = 13,                   /**< Adam optimizer parameter update step. */
    COMMAND_SOFTMAX_BACKWARD = 14,              /**< Backward pass for softmax. */
    COMMAND_MUL_BACKWARD = 15,                  /**< Backward pass for element-wise multiplication. */
    COMMAND_TRANSPOSE_BACKWARD = 16,            /**< Backward pass for basic 2D transpose (which is another transpose). */
    COMMAND_EMBEDDING_LOOKUP = 17,              /**< Embedding table lookup using indices. */
    COMMAND_EMBEDDING_BACKWARD_PASS1 = 18,      /**< Embedding backward: Calculate delta gradients (uses local reduction). */
    COMMAND_REDUCE_SUM_AXIS01 = 19,             /**< Reduce sum over first two axes (B, M) of a (B, M, N) tensor, output (N). Used for bias gradient. */
    COMMAND_BROADCAST_ADD_BIAS = 20,            /**< Broadcast add bias vector (N) to tensor (B, M, N). */
    COMMAND_TRANSPOSE_BATCHED = 21,             /**< Transpose the last two dimensions of a batched tensor (..., D1, D2) -> (..., D2, D1). */
    COMMAND_MATRIX_MULTIPLY_BATCHED = 22,       /**< Batched matrix multiply (C[b] = A[b] @ B[b]). */
    COMMAND_MATRIX_MULTIPLY_BATCHED_BACKWARD_DA = 23, /**< Backward pass for batched matmul, calculating gradient dA. */
    COMMAND_MATRIX_MULTIPLY_BATCHED_BACKWARD_DB = 24, /**< Backward pass for batched matmul, calculating gradient dB. */
    COMMAND_TRANSPOSE_12_BATCHED = 25,          /**< Transpose dimensions 1 and 2 of a 4D tensor (B, D1, D2, D3) -> (B, D2, D1, D3). */
    COMMAND_LOG_SOFTMAX_STABLE = 26,            /**< Row-wise numerically stable log-softmax. */
    COMMAND_CROSS_ENTROPY_LOSS_GRAD = 27,       /**< Calculate cross-entropy loss and gradient w.r.t. logits (input expected to be log-probabilities). */
    COMMAND_ADD_BROADCAST_PE = 28,              /**< Broadcast add positional encoding (S, E) to input (B, S, E). */
    COMMAND_HEBBIAN_OUTER_PRODUCT_UPDATE = 29,  /**< Hebbian weight update using outer product (uses local reduction). */
    COMMAND_THRESHOLD_SPIKE = 30,               /**< Generate binary spikes (0 or 1) based on thresholding activations. */
    COMMAND_ADD_BIAS_MN = 31,                   /**< Add Bias Vector (N) to Matrix (M, N). */
    COMMAND_DYNAMIC_TOKEN_ASSIGNMENT = 32,      /**< Assign activation vector to the closest prototype based on dot product similarity. */
    COMMAND_PAIRWISE_SIMILARITY = 33,           /**< Compute pairwise similarity matrix (dot product) between state vectors. */
    COMMAND_PROTO_SEGMENTED_SUM = 34,           /**< Atomically sum activations per prototype based on indices (Requires Atomics). */
    COMMAND_PROTO_UPDATE_STEP = 35,             /**< Update prototypes using accumulated sums and counts from segmented sum. */
    COMMAND_SHAPE_LOSS_REWARD_PENALTY = 36,     /**< Adjust loss based on reward/penalty rules (single pair). */
    COMMAND_SHAPE_LOSS_REWARD_PENALTY_LIST = 37,/**< Adjust loss based on reward/penalty rules (list of pairs). */ // NEU
    COMMAND_FUSED_DIFFUSION = 38,               /**< Diffusion step combining self-retention and weighted neighbor aggregation. */
    COMMAND_IZHIKEVICH_STEP = 39,               /**< Advance Izhikevich neuron dynamics for one time step. */
    COMMAND_STDP_UPDATE = 40,                   /**< Apply spike-timing-dependent plasticity weight updates. */
    COMMAND_STDP_TRACE_UPDATE = 41,             /**< Decay and refresh STDP spike traces. */
    COMMAND_LBM_COLLIDE_STREAM = 42,            /**< Perform a Lattice-Boltzmann collide-and-stream step (D2Q9). */
    COMMAND_NBODY_FORCES = 43,                  /**< Compute pairwise gravitational forces for N-body simulation. */
    COMMAND_NBODY_INTEGRATE = 44,               /**< Integrate N-body positions and velocities from computed forces. */
    COMMAND_ISING_METROPOLIS = 45,              /**< Metropolis update for checkerboard Ising simulation step. */
    COMMAND_CONV2D_FORWARD = 46,               /**< 2D convolution forward pass (NCHW). */
    COMMAND_CONV2D_BACKWARD = 47,               /**< 2D convolution backward pass computing dInput, dWeight, dBias. */
    COMMAND_PATCH_PERMUTE_RESHAPE = 48,         /**< Permute (0,3,2,1) and fuse reshape for patch embeddings. */
    COMMAND_PATCH_PERMUTE_RESHAPE_BACKWARD = 49 /**< Backward permutation for patch embedding reshape. */
} GPUCommand;

// --- Forward Declarations for Exported Functions ---
DLLEXPORT void set_quantum_enabled(int enabled);
DLLEXPORT int initialize_gpu(int gpu_index);
DLLEXPORT void *allocate_gpu_memory(int gpu_index, size_t size);
DLLEXPORT void free_gpu_memory(int gpu_index, void* buffer_handle);
DLLEXPORT int write_host_to_gpu_blocking(int gpu_index, void* gpu_buffer_handle, size_t offset, size_t size, const void* host_source_ptr);
DLLEXPORT int read_gpu_to_host_blocking(int gpu_index, void* gpu_buffer_handle, size_t offset, size_t size, void* host_destination_ptr);
DLLEXPORT unsigned int simulated_get_compute_unit_count(int gpu_index); // Kept for dummy mode
DLLEXPORT void shutdown_gpu(int gpu_index);
DLLEXPORT int finish_gpu(int gpu_index);

// Kernel Execution Function Exports
DLLEXPORT int execute_matmul_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int B, int M, int N, int K);
DLLEXPORT int execute_softmax_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int num_rows, int row_size);
DLLEXPORT int execute_gelu_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int num_elements);
DLLEXPORT int execute_add_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int num_elements);
DLLEXPORT int execute_add_bias_on_gpu(int gpu_index, void* buffer_a_or_c, void* buffer_b_bias, int M, int N);
DLLEXPORT int execute_mul_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int num_elements);
DLLEXPORT int execute_layernorm_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int num_rows, int row_size, float eps);
DLLEXPORT int execute_clone_on_gpu(int gpu_index, void* src_buffer, void* dst_buffer, size_t size);
DLLEXPORT int execute_transpose_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int rows, int cols);
DLLEXPORT int execute_gelu_backward_on_gpu(int gpu_index, void* buffer_input, void* buffer_grad_output, void* buffer_grad_input, int num_elements);
DLLEXPORT int execute_matmul_backward_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_dc, void* buffer_da, void* buffer_db, int B, int M, int N, int K);
DLLEXPORT int execute_layernorm_backward_on_gpu(int gpu_index, void* buffer_dy, void* buffer_x, void* buffer_dx, int num_rows, int row_size, float eps);
DLLEXPORT int execute_adam_update_on_gpu(int gpu_index, void* param_buffer, void* grad_buffer, void* m_buffer, void* v_buffer, int num_elements, int t, float lr, float beta1, float beta2, float eps, float weight_decay);
DLLEXPORT int execute_softmax_backward_on_gpu(int gpu_index, void* buffer_dy, void* buffer_y, void* buffer_dx, int num_rows, int row_size);
DLLEXPORT int execute_mul_backward_on_gpu(int gpu_index, void* buffer_dC, void* buffer_A, void* buffer_B, void* buffer_dA, void* buffer_dB, int num_elements);
DLLEXPORT int execute_transpose_backward_on_gpu(int gpu_index, void* buffer_dC, void* buffer_dA, int rows_A, int cols_A);
DLLEXPORT int execute_embedding_lookup_gpu(int gpu_index, void* idx, void* w, void* o, int b, int s, int d, int v);
DLLEXPORT int execute_embedding_backward_gpu(int gpu_index, void* d_o, void* idx, void* d_w, int b, int s, int d, int v);
DLLEXPORT int execute_reduce_sum_gpu(int gpu_index, void* in, void* out, int B, int M, int N);
DLLEXPORT int execute_broadcast_add_gpu(int gpu_index, void* a, void* b, void* c, int B, int M, int N);
DLLEXPORT int execute_transpose_batched_gpu(int gpu_index, void* in, void* out, int B_flat, int d1, int d2);
DLLEXPORT int execute_transpose_12_batched_gpu(int gpu_index, void* buffer_in, void* buffer_out, int B, int D1, int D2, int D3);
DLLEXPORT int execute_matmul_batched_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int B, int M, int N, int K);
DLLEXPORT int execute_matmul_batched_backward_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_dc, void* buffer_da, void* buffer_db, int B, int M, int N, int K);
DLLEXPORT int execute_log_softmax_stable_gpu(int gpu_index, void* input_logits, void* output_log_probs, int B_S_rows, int V_cols);
DLLEXPORT int execute_cross_entropy_loss_grad_gpu(int gpu_index, void* log_probs, void* target_indices, void* grad_input, void* loss_per_sample, int num_rows, int V);
DLLEXPORT int execute_add_broadcast_pe_gpu(int gpu_index, void* input, void* pe_slice, void* output, int B, int S, int E);
DLLEXPORT int execute_hebbian_update_on_gpu(int gpu_index, void* buffer_a, void* buffer_c, void* buffer_w, float learning_rate, int B, int M, int N, int K);
DLLEXPORT int execute_threshold_spike_on_gpu(int gpu_index, void* buffer_activations, void* buffer_spikes, float threshold, int num_elements);
DLLEXPORT int execute_dynamic_token_assignment_gpu(int gpu_index, void* activations_bse, void* prototypes_te, void* output_indices_bs, int B, int S, int E, int T);
DLLEXPORT int execute_pairwise_similarity_gpu(int gpu_index, void* states_nd, void* output_similarity_nn, int N, int D);
DLLEXPORT int execute_proto_segmented_sum_gpu(int gpu_index, void* activations_flat, void* indices_flat, void* proto_sums, void* proto_counts, int num_elements_flat, int E, int T);
DLLEXPORT int execute_proto_update_step_gpu(int gpu_index, void* prototypes, void* proto_sums, void* proto_counts, float learning_rate, int E, int T);
// Loss Shaping Exports
DLLEXPORT int execute_shape_loss_with_reward_penalty_gpu(int gpu_index, void* loss_per_sample_in, void* predictions, void* targets, void* loss_per_sample_out, int num_samples, int num_classes, float penalty_weight, float reward_weight, float high_confidence_threshold, int critical_target_class, int critical_predicted_class);
DLLEXPORT int execute_shape_loss_with_reward_penalty_list_gpu(int gpu_index, void* loss_per_sample_in, void* predictions, void* targets, void* loss_per_sample_out, void* critical_pairs, int num_samples, int num_classes, int num_critical_pairs, float penalty_weight, float reward_weight, float high_confidence_threshold); // NEU
DLLEXPORT int execute_fused_diffusion_on_gpu(int gpu_index, void* buffer_X, void* buffer_W, void* buffer_O, int B, int N, int D, float gamma, float sigma);
DLLEXPORT int execute_izhikevich_step_on_gpu(int gpu_index, void* v, void* u, void* i_inj, void* spikes_out, void* p_a, void* p_b, void* p_c, void* p_d, float dt, float threshold, int num_neurons);
DLLEXPORT int execute_stdp_update_on_gpu(int gpu_index, void* weights, void* pre_traces, void* post_traces, void* pre_spike_events, void* post_spike_events, float lr_ltp, float lr_ltd, int pre_n, int post_n);
DLLEXPORT int execute_stdp_trace_update_on_gpu(int gpu_index, void* pre_traces, void* post_traces, void* pre_spike_events, void* post_spike_events, float decay_pre, float decay_post, float increment_pre, float increment_post, int pre_n, int post_n);
DLLEXPORT int execute_lbm_collide_and_stream_on_gpu(int gpu_index, void* f_in, void* f_out, void* rho, void* ux, void* uy, float omega, int width, int height);
DLLEXPORT int execute_nbody_calculate_forces_on_gpu(int gpu_index, void* positions, void* forces, float gravitational_const, float softening_factor, int num_bodies);
DLLEXPORT int execute_nbody_integrate_on_gpu(int gpu_index, void* positions, void* velocities, void* forces, float dt, int num_bodies);
DLLEXPORT int execute_ising_metropolis_step_on_gpu(int gpu_index, void* spin_grid, void* random_numbers, float J, float beta, int width, int height, int color);
DLLEXPORT int sqse_load_kernels(const char* kernel_path);
DLLEXPORT int execute_sqse_encrypt_float(const float* data_in,
                                         const float* key,
                                         int n,
                                         float chaos_K,
                                         int steps,
                                         float* out_theta,
                                         float* out_p_masked);
DLLEXPORT int execute_sqse_decrypt_float(const float* in_theta,
                                         const float* in_p_masked,
                                         const float* key,
                                         int n,
                                         float chaos_K,
                                         int steps,
                                         float* data_out);
DLLEXPORT void set_noise_level(int gpu_index, float value);
DLLEXPORT float get_noise_level(int gpu_index);
DLLEXPORT void register_kernel_measurement_buffers(float* error_ptr, float* variance_ptr);
DLLEXPORT void reset_kernel_measurement_buffers(void);
DLLEXPORT int get_last_kernel_metrics(int gpu_index, KernelMetricsSample* out_metrics);
DLLEXPORT int subqg_initialize_state(int gpu_index, float initial_energy, float initial_phase, float noise_level, float threshold);
DLLEXPORT int subqg_initialize_state_batched(int gpu_index, int cell_count,
                                             const float* initial_energy, const float* initial_phase,
                                             float noise_level, float threshold);
DLLEXPORT int subqg_simulation_step(int gpu_index, float rng_energy, float rng_phase, float rng_spin,
                                    float* out_energy, float* out_phase, float* out_interference,
                                    int* out_node_flag, int* out_spin, int* out_topology,
                                    float* out_field_map, int field_map_length);
DLLEXPORT int subqg_simulation_step_batched(int gpu_index,
                                            const float* rng_energy, const float* rng_phase, const float* rng_spin,
                                            int batch_count,
                                            float* out_energy, float* out_phase, float* out_interference,
                                            int* out_node_flag, int* out_spin, int* out_topology,
                                            float* out_field_map, int field_map_length);
DLLEXPORT void subqg_set_deterministic_mode(int enabled, uint64_t seed);
DLLEXPORT void subqg_release_state(int gpu_index);
DLLEXPORT int subqg_inject_agents(int gpu_index, const HPIOAgent* agents, int count);
DLLEXPORT void subqg_set_params(float noise_level, float threshold);

// Mycel / pheromone hybrid exports
#ifdef __cplusplus
extern "C" {
#endif

DLLEXPORT int subqg_init_mycel(int gpu_index, int T_cap, int C, int K);
DLLEXPORT int subqg_set_active_T(int gpu_index, int T_act);

/* Size-probe fähig: out==NULL oder bytes==0 → gibt nur die benötigten Bytes zurück. */
DLLEXPORT int read_full_pheromone_buffer(void* out_buffer, int out_bytes);

#ifdef __cplusplus
} /* extern "C" */
#endif

DLLEXPORT int  subqg_realloc_pheromone_channels(int gpu_index, int new_C);
DLLEXPORT int  subqg_set_repro_params(int gpu_index, float thr_nu, float thr_act, float mut_sigma);
DLLEXPORT int  subqg_set_nutrient_recovery(int gpu_index, float recovery_rate);
DLLEXPORT int  set_pheromone_gains(int gpu_index, const float* gain_C, int count);
DLLEXPORT int  set_diffusion_params(int gpu_index, float decay_default, float diffu_default);
DLLEXPORT int  set_neighbors_sparse(int gpu_index, const int* neigh_idx_TK);
DLLEXPORT int  set_mood_state(int gpu_index, const float* mood_tC);
DLLEXPORT int  set_nutrient_state(int gpu_index, const float* nutrient_t);
DLLEXPORT int  step_pheromone_reinforce(int gpu_index, const float* activity_t);
DLLEXPORT int  step_pheromone_diffuse_decay(int gpu_index);
DLLEXPORT int  step_mycel_update(int gpu_index, const float* activity_t);
DLLEXPORT int  step_colony_update(int gpu_index, int iterations);
DLLEXPORT int  step_reproduction(int gpu_index, const float* activity_t, const float* prototypes, int E);
DLLEXPORT int  step_subqg_feedback(int gpu_index, float kappa_nutrient, const float* kappa_mood, int count);
DLLEXPORT int  step_potential_for_hpio(int gpu_index, const float* mood_weights, int count);
DLLEXPORT int  read_pheromone_slice(int gpu_index, int channel, float* out_TK);
DLLEXPORT int  render_frame_to_buffer(int gpu_index, int width, int height,
                                      void* out_buffer_host,
                                      const RenderAgent* agents, int num_agents,
                                      const Vec2f* trail_points, int num_trail_points,
                                      float exposure_scale, float agent_radius,
                                      float trail_thickness, float clip_percentile);
DLLEXPORT int  read_nutrient(int gpu_index, float* out_T);
DLLEXPORT int  read_potential(int gpu_index, float* out_T);
DLLEXPORT int  read_colonies(int gpu_index, uint8_t* out_T);
DLLEXPORT int  save_mycel_state(int gpu_index, const char* path);
DLLEXPORT int  load_mycel_state(int gpu_index, const char* path);

// Quantum algorithm support structures and exports
typedef struct {
    uint64_t z_mask;
    float coefficient;
} PauliZTerm;

DLLEXPORT int execute_shor_gpu(int gpu_index, int modulus_N, int base_a,
                               int* out_period_estimate,
                               float* out_control_distribution, int distribution_length);
DLLEXPORT int execute_grover_gpu(int gpu_index, int num_qubits, int iterations,
                                 uint64_t marked_mask, uint64_t marked_value,
                                 int* out_marked_state,
                                 float* out_distribution, int distribution_length);
DLLEXPORT int execute_vqe_gpu(int gpu_index, int num_qubits, int ansatz_layers,
                              const float* parameters, int num_parameters,
                              const PauliZTerm* hamiltonian_terms, int num_terms,
                              float* out_energy, float* out_gradients);
DLLEXPORT int execute_vqe_gradients_parallel_gpu(int gpu_index, int num_qubits, int ansatz_layers,
                                                 const float* parameters, int num_parameters,
                                                 const PauliZTerm* hamiltonian_terms, int num_terms,
                                                 float* out_energy, float* out_gradients);
DLLEXPORT int execute_qaoa_gpu(int gpu_index, int num_qubits, int p_layers,
                               const float* gammas, const float* betas, int num_parameters,
                               const PauliZTerm* cost_terms, int num_cost_terms,
                               float* out_energy);
DLLEXPORT int execute_hhl_gpu(int gpu_index, const float* matrix_A, const float* vector_b,
                              int system_size, float* out_solution, int solution_length);
DLLEXPORT int execute_qml_classifier_gpu(int gpu_index, int num_qubits,
                                         const float* feature_vector, int num_features,
                                         const float* parameters, int num_parameters,
                                         float* out_expectations, int expectation_length);
DLLEXPORT int execute_qec_cycle_gpu(int gpu_index, int code_type, uint32_t error_mask,
                                    float* out_syndrome, int syndrome_length);
DLLEXPORT int quantum_upload_gate_sequence(int gpu_index, const QuantumGate* gates, int gate_count);
DLLEXPORT int quantum_apply_gate_sequence(int gpu_index, int num_qubits, float* out_probabilities, int probability_length);
DLLEXPORT int quantum_export_to_qasm(int gpu_index, const char* filepath);
/**
 * @brief Execute the GPU-based quantum echo / OTOC(2) protocol.
 *
 * The function initializes the |0…0⟩ state, applies the forward unitary sequence U,
 * introduces a local perturbation W, and evolves back with U† to estimate the Loschmidt
 * echo L. Optionally, it evaluates the second-order out-of-time-ordered correlator by
 * executing the extended sequence involving an additional observer gate V.
 *
 * @note The @p gpu_index parameter participates in the emerging multi-device manager.
 *       Commands still execute on the shared kernel set, but the function resolves the
 *       command queue via the slot table when available and otherwise falls back to the
 *       global queue/context until full per-device compilation is introduced.
 *
 * @param gpu_index       Target GPU index (reserved, see note).
 * @param num_qubits      Number of qubits for the simulated register.
 * @param U_gates         Pointer to the forward evolution gate list U (may be NULL when
 *                        @p U_gate_count is zero).
 * @param U_gate_count    Number of entries contained in @p U_gates.
 * @param W_gate          Pointer to the perturbation gate descriptor W (must not be NULL).
 * @param V_gate          Optional observer gate descriptor V (can be NULL when unused).
 * @param measure_otoc2   Flag selecting whether to evaluate the OTOC(2) branch.
 * @param out_L           Output pointer receiving |⟨0…0|ψ_final⟩|² after U†WU.
 * @param out_otoc2_real  Output pointer for the real part of OTOC(2) (required when
 *                        @p measure_otoc2 is non-zero).
 * @param out_otoc2_imag  Output pointer for the imaginary part of OTOC(2) (required when
 *                        @p measure_otoc2 is non-zero).
 *
 * @return 1 on success, 0 on failure. On error, diagnostic messages are written to stderr
 *         and the OpenCL command queue is drained via finish_queue_and_check().
 */
DLLEXPORT int execute_quantum_echoes_otoc_gpu(
    int gpu_index,
    int num_qubits,
    const QuantumGate* U_gates,
    int U_gate_count,
    const QuantumGate* W_gate,
    const QuantumGate* V_gate,
    int measure_otoc2,
    float* out_L,
    float* out_otoc2_real,
    float* out_otoc2_imag);

// --- Internal Helper Function Declarations ---
cl_int compile_opencl_kernel_variant(const char* kernel_source, const char* kernel_name,
                                     cl_program* program_out, cl_kernel* kernel_out,
                                     int enable_fast_math);
cl_int compile_opencl_kernel_dual(const char* kernel_source, const char* kernel_name,
                                  cl_program* strict_program_out, cl_kernel* strict_kernel_out,
                                  cl_program* fast_program_out, cl_kernel* fast_kernel_out);
int submit_kernel_command(int gpu_index, GPUCommand command, void *data);
int finish_queue_and_check(int gpu_index, const char* func_name);
void shutdown_driver();
unsigned int get_compute_unit_count(int gpu_index);
int zero_gpu_buffer(int gpu_index, void* gpu_buffer_handle, size_t size_bytes);
static cl_int get_reduction_params_helper(size_t* lws_out, size_t* local_mem_bytes_out);
static void release_subqg_resources(void);
static void release_quantum_program_objects(void);
static void release_quantum_resources(void);
static cl_int enqueue_kernel_with_metrics(cl_kernel kernel,
                                          cl_uint work_dim,
                                          const size_t* global_work_size,
                                          const size_t* local_work_size,
                                          const char* kernel_name,
                                          float* error_out,
                                          float* variance_out);
DLLEXPORT const char* cc_get_last_error(void);
DLLEXPORT const char* cc_get_version(void);

#define ENQUEUE_KERNEL_PROFILED(kernel_handle, work_dim, global_ptr, local_ptr, kernel_label) \
    enqueue_kernel_with_metrics(kernel_handle, work_dim, global_ptr, local_ptr, kernel_label, NULL, NULL)

// Quantum helper declarations
typedef struct {
    cl_mem buffer;
    int num_qubits;
    size_t dimension;
} QuantumStateGPU;

typedef struct QuantumEchoProfile {
    uint64_t single_qubit_gate_count;
    uint64_t two_qubit_gate_count;
    uint64_t three_qubit_gate_count;
    uint64_t fused_single_gate_groups;
    uint64_t total_gate_applications;
    uint64_t estimated_global_mem_bytes;
    uint64_t kernel_enqueue_count;
    double   host_wall_time_ms;
    int      used_out_of_order_queue;
} QuantumEchoProfile;

static QuantumEchoProfile g_last_quantum_echo_profile = {0};
static QuantumEchoProfile* g_active_quantum_profile = NULL;

DLLEXPORT int get_last_quantum_echo_profile(QuantumEchoProfile* out_profile);

static int ensure_sqse_kernels_ready(void);
static cl_float2 make_complex(float real, float imag);
static int ensure_quantum_kernels_ready(void);
static int quantum_allocate_state(int num_qubits, QuantumStateGPU* state_out);
static void quantum_release_state(QuantumStateGPU* state);
static int quantum_initialize_zero_state(QuantumStateGPU* state);
static int quantum_apply_single_qubit_gate(QuantumStateGPU* state, int target,
                                           cl_float2 g00, cl_float2 g01, cl_float2 g10, cl_float2 g11);
static int quantum_apply_hadamard(QuantumStateGPU* state, int target);
static int quantum_apply_pauli_x(QuantumStateGPU* state, int target);
static int quantum_apply_rotation_x(QuantumStateGPU* state, int target, float theta);
static int quantum_apply_rotation_z(QuantumStateGPU* state, int target, float theta);
static int quantum_apply_rotation_y(QuantumStateGPU* state, int target, float theta);
static int quantum_apply_pauli_z(QuantumStateGPU* state, int target);
static int quantum_apply_pauli_y(QuantumStateGPU* state, int target);
static int quantum_apply_controlled_phase(QuantumStateGPU* state, int control, int target, float theta);
static int quantum_apply_controlled_not(QuantumStateGPU* state, int control, int target);
static int quantum_swap_qubits_out_of_place(QuantumStateGPU* state, int q1, int q2);
static int quantum_inverse_qft(QuantumStateGPU* state, int start_qubit, int count);
static int quantum_apply_modular_exponentiation(QuantumStateGPU* state, int num_control, int num_work, int base_a, int modulus_N);
static int quantum_prepare_uniform_superposition(QuantumStateGPU* state, int num_qubits_to_prepare, int start_qubit);
static int quantum_apply_grover_oracle(QuantumStateGPU* state, uint64_t mask, uint64_t value);
static int quantum_apply_grover_diffusion(QuantumStateGPU* state);
static int quantum_compute_probabilities_gpu(QuantumStateGPU* state, cl_mem* probs_out);
static int quantum_expectation_pauli_z_gpu(QuantumStateGPU* state, uint64_t z_mask, float* out_value);
static int quantum_measure_most_probable(QuantumStateGPU* state, int* out_index);
static int quantum_prepare_feature_map(QuantumStateGPU* state, const float* feature_vector, int num_features);
static int quantum_apply_qml_classifier_layer(QuantumStateGPU* state, const float* parameters, int num_qubits);
static uint32_t round_up_to_power_of_two(uint32_t value);
static int quantum_reserve_temp_state(size_t dimension);
static int quantum_reserve_probability_buffer(size_t dimension);
static uint64_t host_modexp_uint64(uint64_t base, uint64_t exp, uint64_t mod);
static int quantum_apply_vqe_ansatz(QuantumStateGPU* state, int num_qubits, int ansatz_layers, const float* parameters, int num_parameters);
static int quantum_compute_pauli_z_energy(QuantumStateGPU* state, const PauliZTerm* terms, int num_terms, float* out_energy);
static int quantum_apply_multi_qubit_z_phase(QuantumStateGPU* state, uint64_t mask, float angle);
static int solve_linear_system(const float* matrix, const float* vector, int n, float* solution);
static int quantum_initialize_basis_superposition(QuantumStateGPU* state, const uint32_t* basis_states, size_t count);
static int quantum_prepare_steane_zero_state(QuantumStateGPU* state);
static int quantum_measure_x_parity_gpu(QuantumStateGPU* state, const int* qubits, int count, float* out_value);
static int quantum_apply_gate_cpu(cl_float2* state, int num_qubits, const QuantumGate* gate);
static int quantum_apply_gate_from_desc(QuantumStateGPU* state, const QuantumGate* gate);
static int quantum_apply_sequence(QuantumStateGPU* state, const QuantumGate* seq, int count);
static int quantum_apply_sequence_dagger(QuantumStateGPU* state, const QuantumGate* seq, int count);
static int quantum_apply_gate_dagger(QuantumStateGPU* state, const QuantumGate* gate);
static int quantum_apply_swap_via_cnot(QuantumStateGPU* state, int q1, int q2);
static int quantum_apply_toffoli_decomposed(QuantumStateGPU* state, int control1, int control2, int target);
static int quantum_apply_controlled_rz_decomposed(QuantumStateGPU* state, int control, int target, float theta);
static int quantum_apply_controlled_rx_decomposed(QuantumStateGPU* state, int control, int target, float theta);
static int quantum_apply_controlled_ry_decomposed(QuantumStateGPU* state, int control, int target, float theta);
#ifndef NDEBUG
static int quantum_check_norm1(int gpu_index, QuantumStateGPU* state, float eps, const char* stage);
#endif


// --- Kernel Source Code Strings ---
// (Alle bisherigen Kernel-Strings bleiben hier unverändert eingefügt)

const char *render_kernel_src =
"// ----------------------------------------------------------------\n"
"// GPU Frame Rendering Kernels\n"
"// ----------------------------------------------------------------\n"
"typedef struct { float x; float y; } Vec2f;\n"
"typedef struct {\n"
"    float pos_x;\n"
"    float pos_y;\n"
"    float hue;\n"
"    int trail_start;\n"
"    int trail_len;\n"
"} RenderAgent;\n"
"\n"
"#if defined(__OPENCL_VERSION__) && defined(__clang__)\n"
"#define CC_INLINE_FN static inline __attribute__((always_inline))\n"
"#else\n"
"#define CC_INLINE_FN static inline\n"
"#endif\n"
"\n"
"CC_INLINE_FN float clamp01(float v){ return fmax(0.0f, fmin(1.0f, v)); }\n"
"\n"
"CC_INLINE_FN float3 hue_to_rgb(float h){\n"
"    h = h - floor(h);\n"
"    float c = 1.0f;\n"
"    float x = c * (1.0f - fabs(fmod(h * 6.0f, 2.0f) - 1.0f));\n"
"    float3 rgb;\n"
"    if (h < 1.0f/6.0f)      rgb = (float3)(c,x,0.0f);\n"
"    else if (h < 2.0f/6.0f) rgb = (float3)(x,c,0.0f);\n"
"    else if (h < 3.0f/6.0f) rgb = (float3)(0.0f,c,x);\n"
"    else if (h < 4.0f/6.0f) rgb = (float3)(0.0f,x,c);\n"
"    else if (h < 5.0f/6.0f) rgb = (float3)(x,0.0f,c);\n"
"    else                    rgb = (float3)(c,0.0f,x);\n"
"    return clamp(rgb, (float3)(0.0f), (float3)(1.0f));\n"
"}\n"
"\n"
"CC_INLINE_FN float4 blend_over(float4 dst, float3 src_rgb, float a){\n"
"    a = clamp01(a);\n"
"    float ia = 1.0f - a;\n"
"    float3 out = dst.xyz * ia + src_rgb * a;\n"
"    return (float4)(clamp(out.x, 0.0f, 1.0f), clamp(out.y, 0.0f, 1.0f), clamp(out.z, 0.0f, 1.0f), 1.0f);\n"
"}\n"
"\n"
"CC_INLINE_FN float sample_subqg(float px, float py, int W, int H, int FW, int FH, __global const float* field, int len){\n"
"    if (field == 0 || len <= 0) { return 0.0f; }\n"
"    int eff_FW = (FW > 0) ? FW : 1;\n"
"    int eff_FH = (FH > 0) ? FH : 1;\n"
"    int max_px = (W > 0) ? (W - 1) : 0;\n"
"    int max_py = (H > 0) ? (H - 1) : 0;\n"
"    float clamped_px = clamp(px, 0.0f, (float)max_px);\n"
"    float clamped_py = clamp(py, 0.0f, (float)max_py);\n"
"    float u = (W > 1) ? (clamped_px / (float)(W - 1)) : 0.0f;\n"
"    float v = (H > 1) ? (clamped_py / (float)(H - 1)) : 0.0f;\n"
"    float fx = (eff_FW > 1) ? clamp(u * (float)(eff_FW - 1), 0.0f, (float)(eff_FW - 1)) : 0.0f;\n"
"    float fy = (eff_FH > 1) ? clamp(v * (float)(eff_FH - 1), 0.0f, (float)(eff_FH - 1)) : 0.0f;\n"
"    int x0 = (int)floor(fx);\n"
"    int y0 = (int)floor(fy);\n"
"    int x1 = min(x0 + 1, eff_FW - 1);\n"
"    int y1 = min(y0 + 1, eff_FH - 1);\n"
"    float tx = fx - (float)x0;\n"
"    float ty = fy - (float)y0;\n"
"    int i00 = y0 * eff_FW + x0;\n"
"    int i10 = y0 * eff_FW + x1;\n"
"    int i01 = y1 * eff_FW + x0;\n"
"    int i11 = y1 * eff_FW + x1;\n"
"    float h00 = (i00 < len) ? field[i00] : 0.0f;\n"
"    float h10 = (i10 < len) ? field[i10] : 0.0f;\n"
"    float h01 = (i01 < len) ? field[i01] : 0.0f;\n"
"    float h11 = (i11 < len) ? field[i11] : 0.0f;\n"
"    float hx0 = mad(tx, (h10 - h00), h00);\n"
"    float hx1 = mad(tx, (h11 - h01), h01);\n"
"    return mad(ty, (hx1 - hx0), hx0);\n"
"}\n"
"\n"
"CC_INLINE_FN float4 shade_pixel(\n"
"    int px, int py, int W, int H,\n"
"    __global const RenderAgent* agents, int n_agents,\n"
"    __global const Vec2f* trails, int n_trails,\n"
"    float exposure, float agent_radius, float trail_thickness, float clip,\n"
"    __global const float* pheromone,\n"
"    int total_cells, int active_cells, int neighbor_count, int channel_count,\n"
"    __global const float* subqg_field, int subqg_len, int subqg_W, int subqg_H)\n"
"{\n"
"    float inv_neighbors = (neighbor_count > 0) ? (1.0f / (float)neighbor_count) : 1.0f;\n"
"    float inv_exposure = (exposure > 1e-5f) ? (1.0f / exposure) : 1.0f;\n"
"    int cell_idx = py * W + px;\n"
"    float clip_norm = clip;\n"
"    if (clip_norm > 1.0f) { clip_norm *= 0.01f; }\n"
"    clip_norm = clamp01(clip_norm);\n"
"    if (clip_norm <= 0.0f) { clip_norm = 1.0f; }\n"
"    int field_cells = subqg_len;\n"
"    if (active_cells > 0 && active_cells < field_cells) { field_cells = active_cells; }\n"
"    float base_bias = clamp01(0.02f + 0.04f * exposure);\n"
"    float height_center = sample_subqg((float)px, (float)py, W, H, subqg_W, subqg_H, subqg_field, field_cells);\n"
"    float height_norm = tanh(height_center * 0.35f);\n"
"    float grad_x = 0.5f * (sample_subqg((float)px + 1.0f, (float)py, W, H, subqg_W, subqg_H, subqg_field, field_cells)\n"
"                           - sample_subqg((float)px - 1.0f, (float)py, W, H, subqg_W, subqg_H, subqg_field, field_cells));\n"
"    float grad_y = 0.5f * (sample_subqg((float)px, (float)py + 1.0f, W, H, subqg_W, subqg_H, subqg_field, field_cells)\n"
"                           - sample_subqg((float)px, (float)py - 1.0f, W, H, subqg_W, subqg_H, subqg_field, field_cells));\n"
"    float3 normal = (float3)(-grad_x * 3.2f, -grad_y * 3.2f, 1.0f);\n"
"    float inv_len = native_rsqrt(normal.x * normal.x + normal.y * normal.y + normal.z * normal.z + 1e-6f);\n"
"    normal *= inv_len;\n"
"    float3 light_dir = normalize((float3)(-0.45f, -0.55f, 0.70f));\n"
"    float3 half_vec = normalize(light_dir + (float3)(0.0f, 0.0f, 1.0f));\n"
"    float diffuse = fmax(0.0f, dot(normal, light_dir));\n"
"    float spec = pow(fmax(0.0f, dot(normal, half_vec)), 48.0f);\n"
"    float grad_mag = sqrt(grad_x * grad_x + grad_y * grad_y);\n"
"    float foam = clamp01(grad_mag * 2.2f + fmax(0.0f, height_norm - 0.6f) * 1.8f);\n"
"    float mix_t = clamp01(0.5f + 0.5f * height_norm);\n"
"    float3 deep = (float3)(0.050f, 0.090f, 0.200f);\n"
"    float3 shallow = (float3)(0.350f, 0.650f, 0.950f);\n"
"    float3 color = mix(deep, shallow, mix_t);\n"
"    float lighting = 0.25f + diffuse * 0.9f;\n"
"    color = color * lighting + spec * 0.40f;\n"
"    color += foam * (float3)(0.25f, 0.32f, 0.35f);\n"
"    float3 pher_rgb = (float3)(0.0f, 0.0f, 0.0f);\n"
"    if (pheromone && channel_count > 0 && neighbor_count > 0 && cell_idx < total_cells) {\n"
"        int edge_base = cell_idx * neighbor_count;\n"
"        for (int k = 0; k < neighbor_count; ++k) {\n"
"            int edge_idx = edge_base + k;\n"
"            int channel_base = edge_idx * channel_count;\n"
"            if (channel_count >= 1) { pher_rgb.x += fabs(pheromone[channel_base + 0]); }\n"
"            if (channel_count >= 2) { pher_rgb.y += fabs(pheromone[channel_base + 1]); }\n"
"            if (channel_count >= 3) { pher_rgb.z += fabs(pheromone[channel_base + 2]); }\n"
"        }\n"
"        pher_rgb *= inv_neighbors * inv_exposure;\n"
"    }\n"
"    pher_rgb = clamp(pher_rgb, (float3)(0.0f), (float3)(clip_norm));\n"
"    color = clamp(color + pher_rgb * 0.12f + base_bias, (float3)(0.0f), (float3)(1.0f));\n"
"    float4 acc = (float4)(color.x, color.y, color.z, 1.0f);\n"
"\n"
"    float r_trail = fmax(trail_thickness, 0.5f);\n"
"    float r_agent = agent_radius;\n"
"\n"
"    for (int i = 0; i < n_agents; ++i){\n"
"        RenderAgent A = agents[i];\n"
"        if (A.trail_len > 1 && trails && n_trails > 0){\n"
"            float3 col = hue_to_rgb(A.hue);\n"
"            int s = A.trail_start;\n"
"            int e = s + A.trail_len - 1;\n"
"            if (s < 0) { s = 0; }\n"
"            if (e >= n_trails) { e = n_trails - 1; }\n"
"            for (int t = s; t < e; ++t){\n"
"                if (t + 1 >= n_trails) break;\n"
"                float2 p0 = (float2)(trails[t].x, trails[t].y);\n"
"                float2 p1 = (float2)(trails[t+1].x, trails[t+1].y);\n"
"                float2 p = (float2)((float)px + 0.5f, (float)py + 0.5f);\n"
"                float2 v = p1 - p0;\n"
"                float2 w = p - p0;\n"
"                float c1 = dot(w, v);\n"
"                float c2 = dot(v, v);\n"
"                float tt = (c2 > 0.0f) ? clamp01(c1 / c2) : 0.0f;\n"
"                float2 proj = p0 + tt * v;\n"
"                float dx = p.x - proj.x;\n"
"                float dy = p.y - proj.y;\n"
"                float d2 = dx*dx + dy*dy;\n"
"                float r2 = r_trail * r_trail;\n"
"                if (d2 <= r2){\n"
"                    float alpha = 0.35f * (1.0f - clamp01(d2 / r2));\n"
"                    acc = blend_over(acc, col, alpha);\n"
"                }\n"
"            }\n"
"        }\n"
"\n"
"        float2 c = (float2)(A.pos_x, A.pos_y);\n"
"        float dx = ((float)px + 0.5f) - c.x;\n"
"        float dy = ((float)py + 0.5f) - c.y;\n"
"        float d2 = dx*dx + dy*dy;\n"
"        float rr = r_agent * r_agent;\n"
"        if (rr > 0.0f && d2 <= rr){\n"
"            float alpha = 0.95f * (1.0f - clamp01(d2 / rr));\n"
"            float3 col = hue_to_rgb(A.hue);\n"
"            acc = blend_over(acc, col, alpha);\n"
"        }\n"
"    }\n"
"\n"
"    acc.x = fmin(acc.x, clip_norm);\n"
"    acc.y = fmin(acc.y, clip_norm);\n"
"    acc.z = fmin(acc.z, clip_norm);\n"
"    return acc;\n"
"}\n"
"\n"
"#undef CC_INLINE_FN\n"
"\n"
"__kernel void render_frame_img(\n"
"    write_only image2d_t out_img,\n"
"    __global const RenderAgent* agents, int n_agents,\n"
"    __global const Vec2f* trails, int n_trails,\n"
"    int width, int height,\n"
"    float exposure, float agent_radius, float trail_thickness, float clip,\n"
"    __global const float* pheromone,\n"
"    int total_cells, int active_cells, int neighbor_count, int channel_count,\n"
"    __global const float* subqg_field, int subqg_len, int subqg_W, int subqg_H)\n"
"{\n"
"    int gx = get_global_id(0) + get_global_offset(0);\n"
"    int gy = get_global_id(1) + get_global_offset(1);\n"
"    if (gx >= width || gy >= height) return;\n"
"    float4 c = shade_pixel(gx, gy, width, height, agents, n_agents, trails, n_trails,\n"
"                           exposure, agent_radius, trail_thickness, clip,\n"
"                           pheromone, total_cells, active_cells, neighbor_count, channel_count,\n"
"                           subqg_field, subqg_len, subqg_W, subqg_H);\n"
"    write_imagef(out_img, (int2)(gx, gy), c);\n"
"}\n"
"\n"
"__kernel void render_frame_buf(\n"
"    __global uchar4* out_buf,\n"
"    __global const RenderAgent* agents, int n_agents,\n"
"    __global const Vec2f* trails, int n_trails,\n"
"    int width, int height,\n"
"    float exposure, float agent_radius, float trail_thickness, float clip,\n"
"    __global const float* pheromone,\n"
"    int total_cells, int active_cells, int neighbor_count, int channel_count,\n"
"    __global const float* subqg_field, int subqg_len, int subqg_W, int subqg_H)\n"
"{\n"
"    int gx = get_global_id(0) + get_global_offset(0);\n"
"    int gy = get_global_id(1) + get_global_offset(1);\n"
"    if (gx >= width || gy >= height) return;\n"
"    float4 c = shade_pixel(gx, gy, width, height, agents, n_agents, trails, n_trails,\n"
"                           exposure, agent_radius, trail_thickness, clip,\n"
"                           pheromone, total_cells, active_cells, neighbor_count, channel_count,\n"
"                           subqg_field, subqg_len, subqg_W, subqg_H);\n"
"    uint r = (uint)floor(clamp01(c.x) * 255.0f + 0.5f);\n"
"    uint g = (uint)floor(clamp01(c.y) * 255.0f + 0.5f);\n"
"    uint b = (uint)floor(clamp01(c.z) * 255.0f + 0.5f);\n"
"    out_buf[(size_t)gy * (size_t)width + (size_t)gx] = (uchar4)(r, g, b, (uint)255);\n"
"}\n"
"\n"
"__kernel void render_debug(__global uchar4* out, int width, int height) {\n"
"    int x = get_global_id(0);\n"
"    int y = get_global_id(1);\n"
"    if (x >= width || y >= height) return;\n"
"    int idx = y * width + x;\n"
"    int denom_w = (width > 1) ? (width - 1) : 1;\n"
"    int denom_h = (height > 1) ? (height - 1) : 1;\n"
"    uchar r = (uchar)((255 * x) / denom_w);\n"
"    uchar g = (uchar)((255 * y) / denom_h);\n"
"    uchar b = (uchar)128;\n"
"    out[idx] = (uchar4)(r, g, b, (uchar)255);\n"
"}\n";
// Matmul (Standard, Handles 3D @ 2D)
const char *matmul_kernel_src =
"#ifndef M_PI\n"
"#define M_PI 3.14159265358979323846f\n"
"#endif\n"
"__kernel void matrix_multiply(__global const FP_TYPE *a,       /* Input A (B, M, K) or (M, K) */\n"
"                            __global const FP_TYPE *b,       /* Input B (K, N) */\n"
"                            __global FP_TYPE *c,       /* Output C (B, M, N) or (M, N) */\n"
"                            const int B, const int M, const int N, const int K) {\n"
"    int col = get_global_id(0); /* N dimension */\n"
"    int row = get_global_id(1); /* M dimension */\n"
"    int batch_idx = get_global_id(2); /* B dimension */\n"
"\n"
"    /* Check bounds for the output element C[batch_idx, row, col] */\n"
"    if (batch_idx < B && row < M && col < N) {\n"
"        float sum = 0.0f;\n"
"        /* Calculate offset for A based on batch index. If B=1, this offset is 0. */\n"
"        size_t a_batch_offset = (size_t)batch_idx * M * K;\n"
"        /* Calculate offset for C based on batch index. */\n"
"        size_t c_batch_offset = (size_t)batch_idx * M * N;\n"
"\n"
"        /* Perform dot product: sum over k (A[batch, row, k] * B[k, col]) */\n"
"        for (int k = 0; k < K; ++k) {\n"
"             /* Access A using batch offset + row/k indices */\n"
"             /* Access B using standard k/col indices (implicitly broadcasted over B) */\n"
"             sum += (float)a[a_batch_offset + row * K + k] * (float)b[(size_t)k * N + col];\n"
"        }\n"
"        /* Write result to output C */\n"
"        c[c_batch_offset + row * N + col] = (FP_TYPE)sum;\n"
"    }\n"
"}";
// Matmul Backward dA (Standard)
const char *matmul_backward_dA_kernel_src =
"/* dA[b,m,k] = sum_n dC[b,m,n] * B[k,n] (equivalent to dC @ B^T) */\n"
"__kernel void matmul_backward_da(__global const FP_TYPE *dC, /* Gradient dC (B, M, N) */\n"
"                               __global const FP_TYPE *B,  /* Original Input B (K, N) */\n"
"                               __global FP_TYPE *dA, /* Output Gradient dA (B, M, K) */\n"
"                               const int B_dim, const int M_dim, const int N_dim, const int K_dim) {\n"
"    int k = get_global_id(0); /* K dimension */\n"
"    int m = get_global_id(1); /* M dimension */\n"
"    int b = get_global_id(2); /* B dimension */\n"
"\n"
"    /* Bounds check for dA element dA[b, m, k] */\n"
"    if (b < B_dim && m < M_dim && k < K_dim) {\n"
"        float gradient_sum = 0.0f;\n"
"        size_t dc_batch_offset = (size_t)b * M_dim * N_dim;\n"
"        size_t da_batch_offset = (size_t)b * M_dim * K_dim;\n"
"\n"
"        /* Sum over N dimension */\n"
"        for (int n = 0; n < N_dim; ++n) {\n"
"            /* dC[b, m, n] * B[k, n] */\n"
"            gradient_sum += (float)dC[dc_batch_offset + m * N_dim + n] * (float)B[(size_t)k * N_dim + n];\n"
"        }\n"
"        dA[da_batch_offset + m * K_dim + k] = (FP_TYPE)gradient_sum;\n"
"    }\n"
"}";
// Matmul Backward dB (Standard)
const char *matmul_backward_dB_kernel_src =
"/* dB[k,n] = sum_b sum_m A[b,m,k] * dC[b,m,n] (equivalent to A^T @ dC, summed over B) */\n"
"__kernel void matmul_backward_db(__global const FP_TYPE *A,  /* Original Input A (B, M, K) */\n"
"                               __global const FP_TYPE *dC, /* Gradient dC (B, M, N) */\n"
"                               __global FP_TYPE *dB, /* Output Gradient dB (K, N) */\n"
"                               const int B_dim, const int M_dim, const int N_dim, const int K_dim) {\n"
"    int n = get_global_id(0); /* N dimension */\n"
"    int k = get_global_id(1); /* K dimension */\n"
"\n"
"    /* Bounds check for dB element dB[k, n] */\n"
"    if (k < K_dim && n < N_dim) {\n"
"        float gradient_sum = 0.0f;\n"
"        /* Sum over Batch dimension B */\n"
"        for (int b = 0; b < B_dim; ++b) {\n"
"            size_t a_batch_offset = (size_t)b * M_dim * K_dim;\n"
"            size_t dc_batch_offset = (size_t)b * M_dim * N_dim;\n"
"            /* Sum over M dimension */\n"
"            for (int m = 0; m < M_dim; ++m) {\n"
"                /* A[b, m, k] * dC[b, m, n] */\n"
"                gradient_sum += (float)A[a_batch_offset + m * K_dim + k] * (float)dC[dc_batch_offset + m * N_dim + n];\n"
"            }\n"
"        }\n"
"        /* Write the final summed gradient to dB */\n"
"        dB[(size_t)k * N_dim + n] = (FP_TYPE)gradient_sum;\n"
"    }\n"
"}";
// Softmax (Row-wise, Numerically Stable)
const char *softmax_kernel_src =
"#ifndef HUGE_VALF\n"
"#define HUGE_VALF (__builtin_huge_valf())\n"
"#endif\n"
"#ifndef native_exp\n"
"#define native_exp exp\n"
"#endif\n"
"static inline float reduce_max_workgroup(float value, __local float* scratch, int lid, int lsize) {\n"
"    scratch[lid] = value;\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = lsize >> 1; offset > 0; offset >>= 1) {\n"
"        if (lid < offset) {\n"
"            float other = scratch[lid + offset];\n"
"            scratch[lid] = fmax(scratch[lid], other);\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"    return scratch[0];\n"
"}\n"
"static inline float reduce_sum_workgroup(float value, __local float* scratch, int lid, int lsize) {\n"
"    scratch[lid] = value;\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = lsize >> 1; offset > 0; offset >>= 1) {\n"
"        if (lid < offset) {\n"
"            scratch[lid] += scratch[lid + offset];\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"    return scratch[0];\n"
"}\n"
"__kernel void softmax_rowwise(__global const FP_TYPE *input,\n"
"                            __global FP_TYPE *output,\n"
"                            const int num_rows, const int row_size,\n"
"                            __local float* scratch_max, __local float* scratch_sum) {\n"
"    int row = get_group_id(0);\n"
"    if (row >= num_rows) { return; }\n"
"    int lid = get_local_id(0);\n"
"    int lsize = get_local_size(0);\n"
"    size_t offset = (size_t)row * row_size;\n"
"    __global const FP_TYPE* in_row = input + offset;\n"
"    __global FP_TYPE* out_row = output + offset;\n"
"    float local_max = -HUGE_VALF;\n"
"    for (int idx = lid; idx < row_size; idx += lsize) {\n"
"        float v = (float)in_row[idx];\n"
"        local_max = fmax(local_max, v);\n"
"    }\n"
"    float max_val = reduce_max_workgroup(local_max, scratch_max, lid, lsize);\n"
"    float local_sum = 0.0f;\n"
"    for (int idx = lid; idx < row_size; idx += lsize) {\n"
"        float v = (float)in_row[idx];\n"
"        local_sum += native_exp(v - max_val);\n"
"    }\n"
"    float sum_val = reduce_sum_workgroup(local_sum, scratch_sum, lid, lsize);\n"
"    float inv_sum = 1.0f / fmax(sum_val, 1e-9f);\n"
"    for (int idx = lid; idx < row_size; idx += lsize) {\n"
"        float v = (float)in_row[idx];\n"
"        out_row[idx] = (FP_TYPE)(native_exp(v - max_val) * inv_sum);\n"
"    }\n"
"}";
// LogSoftmax (Row-wise, Numerically Stable)
const char *log_softmax_stable_kernel_src =
"#define native_exp exp\n"
"#define native_log log\n"
"#ifndef HUGE_VALF\n"
"#define HUGE_VALF (__builtin_huge_valf())\n"
"#endif\n"
"static inline float reduce_max_workgroup(float value, __local float* scratch, int lid, int lsize) {\n"
"    scratch[lid] = value;\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = lsize >> 1; offset > 0; offset >>= 1) {\n"
"        if (lid < offset) {\n"
"            float other = scratch[lid + offset];\n"
"            scratch[lid] = fmax(scratch[lid], other);\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"    return scratch[0];\n"
"}\n"
"static inline float reduce_sum_workgroup(float value, __local float* scratch, int lid, int lsize) {\n"
"    scratch[lid] = value;\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = lsize >> 1; offset > 0; offset >>= 1) {\n"
"        if (lid < offset) {\n"
"            scratch[lid] += scratch[lid + offset];\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"    return scratch[0];\n"
"}\n"
"__kernel void log_softmax_stable_rowwise(__global const FP_TYPE *input_logits,\n"
"                    __global FP_TYPE *output_log_probs,\n"
"                    const int num_rows, const int row_size,\n"
"                    __local float* scratch_max, __local float* scratch_sum) {\n"
"    int row = get_group_id(0);\n"
"    if (row >= num_rows) { return; }\n"
"    int lid = get_local_id(0);\n"
"    int lsize = get_local_size(0);\n"
"    size_t offset = (size_t)row * row_size;\n"
"    __global const FP_TYPE* in_row = input_logits + offset;\n"
"    __global FP_TYPE* out_row = output_log_probs + offset;\n"
"    float local_max = -HUGE_VALF;\n"
"    for (int idx = lid; idx < row_size; idx += lsize) {\n"
"        float v = (float)in_row[idx];\n"
"        local_max = fmax(local_max, v);\n"
"    }\n"
"    float max_val = reduce_max_workgroup(local_max, scratch_max, lid, lsize);\n"
"    float local_sum = 0.0f;\n"
"    for (int idx = lid; idx < row_size; idx += lsize) {\n"
"        float v = (float)in_row[idx];\n"
"        local_sum += native_exp(v - max_val);\n"
"    }\n"
"    float sum_val = reduce_sum_workgroup(local_sum, scratch_sum, lid, lsize);\n"
"    float log_denom = native_log(fmax(sum_val, 1e-9f));\n"
"    for (int idx = lid; idx < row_size; idx += lsize) {\n"
"        float v = (float)in_row[idx];\n"
"        out_row[idx] = (FP_TYPE)(v - max_val - log_denom);\n"
"    }\n"
"}";
// Cross Entropy Loss + Gradient w.r.t Logits
const char *cross_entropy_loss_grad_kernel_src =
"#ifndef native_exp\n"
"#define native_exp exp\n"
"#endif\n"
"\n"
"/* Calculates loss and gradient for cross-entropy. */\n"
"/* Assumes log_probs input is from a log_softmax operation. */\n"
"/* Target indices are integer class IDs. */\n"
"__kernel void cross_entropy_loss_grad(\n"
"                __global const FP_TYPE* log_probs,      /* Input: Log probabilities (B, S, V) flattened (B*S, V) */\n"
"                __global const int* target_indices,   /* Input: Target class indices (B, S) flattened (B*S,) */\n"
"                __global FP_TYPE* grad_input,         /* Output: Gradient w.r.t logits (B, S, V) flattened (B*S, V) */\n"
"                __global FP_TYPE* loss_per_sample,    /* Output: Loss per sample/token (B, S) flattened (B*S,) */\n"
"                const int num_rows, /* B * S */\n"
"                const int V /* Vocabulary size (row_size) */\n"
"                ) {\n"
"\n"
"     /* Global ID maps to the row (token/sample) index */\n"
"    int row = get_global_id(0); /* Index from 0 to num_rows-1 */\n"
"\n"
"    if (row < num_rows) {\n"
"        size_t base_offset = (size_t)row * V; /* Offset for log_probs and grad_input row */\n"
"        __global const FP_TYPE* log_probs_row = log_probs + base_offset;\n"
"        __global FP_TYPE* grad_input_row = grad_input + base_offset;\n"
"\n"
"        /* Get the target index for this row (sample/token) */\n"
"        int target_idx = target_indices[row];\n"
"\n"
"        /* --- Calculate Gradient: grad = probs - one_hot --- */\n"
"        /* This requires calculating probs = exp(log_probs) */\n"
"        for (int v = 0; v < V; ++v) {\n"
"            float current_log_prob = (float)log_probs_row[v];\n"
"            float current_prob = native_exp(current_log_prob);\n"
"            float grad_val = current_prob; /* Initialize gradient with probability */\n"
"\n"
"            /* Subtract 1.0f if this is the target class index */\n"
"            if (v == target_idx) {\n"
"                grad_val -= 1.0f;\n"
"            }\n"
"            grad_input_row[v] = (FP_TYPE)grad_val;\n"
"        }\n"
"\n"
"        /* --- Calculate Loss: loss = -log_prob[target_idx] --- */\n"
"        /* Ensure target_idx is valid before accessing log_probs */\n"
"        if (target_idx >= 0 && target_idx < V) {\n"
"            float target_log_prob = (float)log_probs_row[target_idx];\n"
"            /* Ensure loss is non-negative using fmax (built-in OpenCL function) */\n"
"            loss_per_sample[row] = (FP_TYPE)(fmax(0.0f, -target_log_prob));\n"
"        } else {\n"
"            /* Handle invalid target index (e.g., padding index like -1 or specific id) */\n"
"            /* Assign 0 loss for invalid/padding targets. */\n"
"            loss_per_sample[row] = (FP_TYPE)(0.0f);\n"
"        }\n"
"    }\n"
"}";
// Softmax Backward
const char *softmax_backward_kernel_src =
"#ifdef CL_HAS_FP64 /* Use double for accumulation if supported */\n"
"    typedef double ACCUM_TYPE;\n"
"    #define ACCUM_CONST(x) (double)(x)\n"
"#else /* Fallback to float accumulation */\n"
"    typedef float ACCUM_TYPE;\n"
"    #define ACCUM_CONST(x) (float)(x)\n"
"#endif\n"
"\n"
"/* Computes dL/dx = (dL/dy - sum(dL/dy * y)) * y */\n"
"__kernel void softmax_backward(__global const FP_TYPE *dy_in, /* Gradient dL/dy (num_rows, row_size) */\n"
"                               __global const FP_TYPE *y,    /* Output of forward softmax y (num_rows, row_size) */\n"
"                               __global FP_TYPE *dx,   /* Output Gradient dL/dx (num_rows, row_size) */\n"
"                               const int num_rows, const int row_size) {\n"
"    int row = get_global_id(0); /* Row index */\n"
"\n"
"    if (row < num_rows) {\n"
"        size_t offset = (size_t)row * row_size;\n"
"        __global const FP_TYPE* dy_row = dy_in + offset;\n"
"        __global const FP_TYPE* y_row = y + offset;\n"
"        __global FP_TYPE* dx_row = dx + offset;\n"
"\n"
"        /* 1. Calculate dot product: sum(dL/dy * y) for this row */\n"
"        ACCUM_TYPE dot_product = ACCUM_CONST(0.0);\n"
"        for (int i = 0; i < row_size; ++i) {\n"
"            dot_product += (ACCUM_TYPE)dy_row[i] * (ACCUM_TYPE)y_row[i];\n"
"        }\n"
"\n"
"        /* 2. Calculate gradient dL/dx for each element in the row */\n"
"        for (int i = 0; i < row_size; ++i) {\n"
"            ACCUM_TYPE dy_val = (ACCUM_TYPE)dy_row[i];\n"
"            ACCUM_TYPE y_val = (ACCUM_TYPE)y_row[i];\n"
"            /* dx_i = (dy_i - dot_product) * y_i */\n"
"            ACCUM_TYPE dx_val = (dy_val - dot_product) * y_val;\n"
"            dx_row[i] = (FP_TYPE)dx_val; /* Cast back to original FP_TYPE */\n"
"        }\n"
"    }\n"
"}";
// GELU Activation (Elementwise)
const char *gelu_kernel_src =
"/* Define constants used by GELU */\n"
"#ifndef M_PI\n"
"#define M_PI 3.14159265358979323846f\n"
"#endif\n"
"#ifndef M_SQRT1_2 /* 1/sqrt(2) */\n"
"#define M_SQRT1_2 0.70710678118654752440f\n"
"#endif\n"
"\n"
"#pragma OPENCL EXTENSION cl_khr_fp64 : enable /* Enable FP64 if available for erf calculation */\n"
"#ifndef native_erf /* Use standard erf if native version is not available/defined */\n"
"#define native_erf erf\n"
"#endif\n"
"\n"
"/* GELU(x) = 0.5 * x * (1 + erf(x / sqrt(2))) */\n"
"__kernel void gelu_elementwise(__global const FP_TYPE *input, /* Input tensor */\n"
"                               __global FP_TYPE *output,      /* Output tensor */\n"
"                               const int num_elements) {\n"
"    /* Total number of elements */\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        float x = (float)input[idx]; /* Read input as float */\n"
"        /* Calculate GELU using native erf if possible */\n"
"        float gelu_val = 0.5f * x * (1.0f + native_erf(x * M_SQRT1_2));\n"
"        output[idx] = (FP_TYPE)gelu_val; /* Write result, cast back to FP_TYPE */\n"
"    }\n"
"}";
// GELU Backward (Elementwise)
const char *gelu_backward_kernel_src =
"/* Define constants used by GELU backward */\n"
"#ifndef M_PI\n"
"#define M_PI 3.14159265358979323846f\n"
"#endif\n"
"#ifndef M_SQRT1_2 /* 1/sqrt(2) */\n"
"#define M_SQRT1_2 0.70710678118654752440f\n"
"#endif\n"
"#ifndef M_1_SQRT2PI /* 1/sqrt(2*pi) - Used in PDF */\n"
"#define M_1_SQRT2PI 0.39894228040143267794f\n"
"#endif\n"
"\n"
"#pragma OPENCL EXTENSION cl_khr_fp64 : enable /* Enable FP64 for erf/exp if available */\n"
"#ifndef native_erf /* Use standard erf if native is not defined */\n"
"#define native_erf erf\n"
"#endif\n"
"#ifndef native_exp /* Use standard exp if native is not defined */\n"
"#define native_exp exp\n"
"#endif\n"
"\n"
"/* dGELU/dx = 0.5 * (1 + erf(x / sqrt(2))) + x * (1/sqrt(2*pi)) * exp(-0.5 * x^2) */\n"
"/*           = CDF(x) + x * PDF(x) */\n"
"/* dL/dx = dL/dy * dGELU/dx */\n"
"__kernel void gelu_backward_elementwise(__global const FP_TYPE *input,       /* Original input x to GELU forward */\n"
"                                       __global const FP_TYPE *grad_output, /* Gradient dL/dy from subsequent layer */\n"
"                                       __global FP_TYPE *grad_input,  /* Output gradient dL/dx */\n"
"                                       const int num_elements) {\n"
"\n"
"     /* Total number of elements */\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        float x = (float)input[idx];       /* Original input value */\n"
"        float dy = (float)grad_output[idx]; /* Incoming gradient */\n"
"\n"
"        /* Calculate CDF term: 0.5 * (1 + erf(x / sqrt(2))) */\n"
"        float cdf_term = 0.5f * (1.0f + native_erf(x * M_SQRT1_2));\n"
"        /* Calculate PDF term: (1/sqrt(2*pi)) * exp(-0.5 * x^2) */\n"
"        float pdf_term = M_1_SQRT2PI * native_exp(-0.5f * x * x);\n"
"        /* Calculate dGELU/dx = CDF(x) + x * PDF(x) */\n"
"        float dgelu_dx = cdf_term + x * pdf_term;\n"
"\n"
"        /* Calculate final gradient: dL/dx = dL/dy * dGELU/dx */\n"
"        grad_input[idx] = (FP_TYPE)(dy * dgelu_dx); /* Write result, cast back to FP_TYPE */\n"
"    }\n"
"}";
// Add (Elementwise) - Used for general add and Embedding Bwd Pass 2
const char *add_kernel_src =
"/* c[i] = a[i] + b[i] */\n"
"__kernel void add_elementwise(__global const FP_TYPE *a, /* Input tensor A */\n"
"                             __global const FP_TYPE *b, /* Input tensor B */\n"
"                             __global FP_TYPE *c, /* Output tensor C */\n"
"                             const int num_elements) { /* Total number of elements */\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        c[idx] = (FP_TYPE)((float)a[idx] + (float)b[idx]); /* Perform addition and cast back */\n"
"    }\n"
"}";
// Multiply (Elementwise)
const char *mul_kernel_src =
"/* c[i] = a[i] * b[i] */\n"
"__kernel void mul_elementwise(__global const FP_TYPE *a, /* Input tensor A */\n"
"                             __global const FP_TYPE *b, /* Input tensor B */\n"
"                             __global FP_TYPE *c, /* Output tensor C */\n"
"                             const int num_elements) { /* Total number of elements */\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        c[idx] = (FP_TYPE)((float)a[idx] * (float)b[idx]); /* Perform multiplication and cast back */\n"
"    }\n"
"}";
// Multiply Backward (Elementwise)
const char *mul_backward_kernel_src =
"/* Computes gradients for elementwise multiplication C = A * B */\n"
"/* dA = dC * B */\n"
"/* dB = dC * A */\n"
"__kernel void mul_backward(__global const FP_TYPE *dC, /* Gradient dL/dC from subsequent layer */\n"
"                         __global const FP_TYPE *A,  /* Original Input A from forward pass */\n"
"                         __global const FP_TYPE *B,  /* Original Input B from forward pass */\n"
"                         __global FP_TYPE *dA, /* Output gradient dL/dA (can be NULL conceptually, but kernel expects a buffer if arg is set) */\n"
"                         __global FP_TYPE *dB, /* Output gradient dL/dB (can be NULL conceptually, but kernel expects a buffer if arg is set) */\n"
"                         const int num_elements) { /* Total number of elements */\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        float dC_val = (float)dC[idx]; /* Incoming gradient */\n"
"        float A_val = (float)A[idx];   /* Original input A */\n"
"        float B_val = (float)B[idx];   /* Original input B */\n"
"\n"
"        /* Calculate gradient w.r.t. A: dA = dC * B */\n"
"        /* Host code MUST ensure only valid buffers are passed if grads are needed. */\n"
"        dA[idx] = (FP_TYPE)(dC_val * B_val);\n"
"\n"
"        /* Calculate gradient w.r.t. B: dB = dC * A */\n"
"        /* Host code MUST ensure only valid buffers are passed if grads are needed. */\n"
"        dB[idx] = (FP_TYPE)(dC_val * A_val);\n"
"    }\n"
"}";
// Layer Normalization (Row-wise)
const char *layernorm_kernel_src =
"/* Define accumulation type based on FP64 support */\n"
"#ifdef CL_HAS_FP64\n"
"    typedef double ACCUM_TYPE;\n"
"    #define ACCUM_CONST(x) (double)(x)\n"
"#else\n"
"    typedef float ACCUM_TYPE;\n"
"    #define ACCUM_CONST(x) (float)(x)\n"
"#endif\n"
"\n"
"#ifndef native_rsqrt /* Use standard rsqrt if native version is not available */\n"
"#define native_rsqrt rsqrt\n"
"#endif\n"
"\n"
"/* Performs Layer Normalization along the last dimension. */\n"
"__kernel void layer_norm(__global const FP_TYPE *input, /* Input tensor (num_rows, row_size) flattened */\n"
"                         __global FP_TYPE *output,      /* Output tensor (num_rows, row_size) flattened */\n"
"                         const int num_rows, const int row_size, const float cl_eps) { /* Epsilon added in C host code */\n"
"    int row = get_global_id(0); /* Row index */\n"
"\n"
"    if (row < num_rows) {\n"
"        size_t offset = (size_t)row * row_size; /* Base offset for this row */\n"
"        __global const FP_TYPE* in_row = input + offset;\n"
"        __global FP_TYPE* out_row = output + offset;\n"
"\n"
"        /* 1. Calculate mean of the row */\n"
"        ACCUM_TYPE mean = ACCUM_CONST(0.0);\n"
"        for (int i = 0; i < row_size; ++i) {\n"
"            mean += (ACCUM_TYPE)in_row[i];\n"
"        }\n"
"        mean /= ACCUM_CONST(row_size);\n"
"\n"
"        /* 2. Calculate variance of the row */\n"
"        ACCUM_TYPE variance = ACCUM_CONST(0.0);\n"
"        for (int i = 0; i < row_size; ++i) {\n"
"            ACCUM_TYPE diff = (ACCUM_TYPE)in_row[i] - mean;\n"
"            variance += diff * diff;\n"
"        }\n"
"        variance /= ACCUM_CONST(row_size);\n"
"\n"
"        /* 3. Calculate inverse standard deviation (with epsilon) */\n"
"        /* Use native_rsqrt for potential performance improvement */\n"
"        ACCUM_TYPE eps_accum = (ACCUM_TYPE)cl_eps;\n"
"        ACCUM_TYPE inv_stddev = native_rsqrt(variance + eps_accum);\n"
"\n"
"        /* 4. Normalize the row: output = (input - mean) * inv_stddev */\n"
"        for (int i = 0; i < row_size; ++i) {\n"
"            out_row[i] = (FP_TYPE)(((ACCUM_TYPE)in_row[i] - mean) * inv_stddev);\n"
"        }\n"
"    }\n"
"}";
// Layer Normalization Backward
const char *layernorm_backward_kernel_src =
"#ifdef CL_HAS_FP64\n"
"    typedef double ACCUM_TYPE;\n"
"    #define ACCUM_CONST(x) (double)(x)\n"
"#else\n"
"    typedef float ACCUM_TYPE;\n"
"    #define ACCUM_CONST(x) (float)(x)\n"
"#endif\n"
"\n"
"#ifndef native_rsqrt\n"
"#define native_rsqrt rsqrt\n"
"#endif\n"
"\n"
"/* Calculates gradient for Layer Normalization (without affine parameters gamma/beta). */\n"
"__kernel void layer_norm_backward(__global const FP_TYPE *dy, /* Gradient dL/dy from subsequent layer */\n"
"                                __global const FP_TYPE *x,  /* Original input x to forward LayerNorm */\n"
"                                __global FP_TYPE *dx, /* Output gradient dL/dx */\n"
"                                const int num_rows, const int row_size, const float cl_eps) {\n"
"    int row = get_global_id(0); /* Row index */\n"
"\n"
"    if (row < num_rows) {\n"
"        size_t offset = (size_t)row * row_size;\n"
"        __global const FP_TYPE* dy_row = dy + offset;\n"
"        __global const FP_TYPE* x_row = x + offset;\n"
"        __global FP_TYPE* dx_row = dx + offset;\n"
"\n"
"        /* --- Recompute mean and variance (needed for backward) --- */\n"
"        ACCUM_TYPE mean = ACCUM_CONST(0.0);\n"
"        for (int i = 0; i < row_size; ++i) { mean += (ACCUM_TYPE)x_row[i]; }\n"
"        mean /= ACCUM_CONST(row_size);\n"
"\n"
"        ACCUM_TYPE variance = ACCUM_CONST(0.0);\n"
"        for (int i = 0; i < row_size; ++i) { ACCUM_TYPE diff = (ACCUM_TYPE)x_row[i] - mean; variance += diff * diff; }\n"
"        variance /= ACCUM_CONST(row_size);\n"
"\n"
"        ACCUM_TYPE eps_accum = (ACCUM_TYPE)cl_eps;\n"
"        ACCUM_TYPE inv_stddev = native_rsqrt(variance + eps_accum); /* 1 / sqrt(var + eps) */\n"
"        ACCUM_TYPE N_accum = ACCUM_CONST(row_size);\n"
"\n"
"        /* --- Calculate intermediate sums needed for the gradient --- */\n"
"        ACCUM_TYPE sum_dy = ACCUM_CONST(0.0);           /* sum(dy) */\n"
"        ACCUM_TYPE sum_dy_xhat = ACCUM_CONST(0.0);    /* sum(dy * x_hat) */\n"
"        /* Calculate x_hat = (x - mean) * inv_stddev on the fly */\n"
"        for (int i = 0; i < row_size; i++) {\n"
"            ACCUM_TYPE x_hat = ((ACCUM_TYPE)x_row[i] - mean) * inv_stddev;\n"
"            ACCUM_TYPE dy_accum = (ACCUM_TYPE)dy_row[i];\n"
"            sum_dy += dy_accum;\n"
"            sum_dy_xhat += dy_accum * x_hat;\n"
"        }\n"
"\n"
"        /* --- Calculate gradient dL/dx for each element --- */\n"
"        /* Formula (simplified, without affine params): */\n"
"        /* dx = (1/N) * inv_stddev * [ N*dy - sum(dy) - x_hat * sum(dy * x_hat) ] */\n"
"        for (int i = 0; i < row_size; i++) {\n"
"            ACCUM_TYPE x_hat = ((ACCUM_TYPE)x_row[i] - mean) * inv_stddev; /* Recompute x_hat */\n"
"            ACCUM_TYPE dy_accum = (ACCUM_TYPE)dy_row[i];\n"
"\n"
"            ACCUM_TYPE term1 = N_accum * dy_accum; /* N * dy_i */\n"
"            ACCUM_TYPE term2 = sum_dy;             /* sum(dy) */\n"
"            ACCUM_TYPE term3 = x_hat * sum_dy_xhat; /* x_hat_i * sum(dy * x_hat) */\n"
"\n"
"            /* Combine terms and scale */\n"
"            ACCUM_TYPE dx_accum = (ACCUM_CONST(1.0) / N_accum) * inv_stddev * (term1 - term2 - term3);\n"
"\n"
"            dx_row[i] = (FP_TYPE)dx_accum; /* Write final gradient */\n"
"        }\n"
"    }\n"
"}";
// Transpose (Basic 2D)
const char *transpose_kernel_src =
"/* 16x16 tiled transpose with padded local memory to avoid bank conflicts. */\n"
"#define TILE_DIM 16\n"
"#define TILE_PAD (TILE_DIM + 1)\n"
"__kernel void transpose(__global const FP_TYPE *input,\n"
"                        __global FP_TYPE *output,\n"
"                        const int rows, const int cols) {\n"
"    __local FP_TYPE tile[TILE_DIM][TILE_PAD]; /* Padding prevents bank conflicts */\n"
"    int block_col = get_group_id(0);\n"
"    int block_row = get_group_id(1);\n"
"    int local_col = get_local_id(0);\n"
"    int local_row = get_local_id(1);\n"
"    int global_col = block_col * TILE_DIM + local_col;\n"
"    int global_row = block_row * TILE_DIM + local_row;\n"
"    if (global_row < rows && global_col < cols) {\n"
"        tile[local_row][local_col] = input[(size_t)global_row * cols + global_col];\n"
"    } else {\n"
"        tile[local_row][local_col] = (FP_TYPE)0;\n"
"    }\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    int transposed_block_col = block_row;\n"
"    int transposed_block_row = block_col;\n"
"    int transposed_col = transposed_block_col * TILE_DIM + local_col;\n"
"    int transposed_row = transposed_block_row * TILE_DIM + local_row;\n"
"    if (transposed_row < cols && transposed_col < rows) {\n"
"        output[(size_t)transposed_row * rows + transposed_col] = tile[local_col][local_row];\n"
"    }\n"
"}\n"
"#undef TILE_PAD\n"
"#undef TILE_DIM\n";
// Transpose Backward (Basic 2D)
const char *transpose_backward_kernel_src =
"/* Backward of transpose Y=X^T is another tiled transpose of the gradient. */\n"
"#define TILE_DIM 16\n"
"#define TILE_PAD (TILE_DIM + 1)\n"
"__kernel void transpose_backward(__global const FP_TYPE *dC,\n"
"                               __global FP_TYPE *dA,\n"
"                               const int rows_A, const int cols_A) {\n"
"    __local FP_TYPE tile[TILE_DIM][TILE_PAD];\n"
"    int block_col = get_group_id(0);\n"
"    int block_row = get_group_id(1);\n"
"    int local_col = get_local_id(0);\n"
"    int local_row = get_local_id(1);\n"
"    int global_col = block_col * TILE_DIM + local_col;\n"
"    int global_row = block_row * TILE_DIM + local_row;\n"
"    if (global_row < cols_A && global_col < rows_A) {\n"
"        tile[local_row][local_col] = dC[(size_t)global_row * rows_A + global_col];\n"
"    } else {\n"
"        tile[local_row][local_col] = (FP_TYPE)0;\n"
"    }\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    int transposed_block_col = block_row;\n"
"    int transposed_block_row = block_col;\n"
"    int transposed_col = transposed_block_col * TILE_DIM + local_col;\n"
"    int transposed_row = transposed_block_row * TILE_DIM + local_row;\n"
"    if (transposed_row < rows_A && transposed_col < cols_A) {\n"
"        dA[(size_t)transposed_row * cols_A + transposed_col] = tile[local_col][local_row];\n"
"    }\n"
"}\n"
"#undef TILE_PAD\n"
"#undef TILE_DIM\n";
// Adam Optimizer Update
const char *adam_kernel_src =
"/* Use standard sqrt if native version is not available */\n"
"#ifndef native_sqrt\n"
"#define native_sqrt sqrt\n"
"#endif\n"
"\n"
"/* Performs Adam weight update step. */\n"
"/* Note: m and v states are expected to be float, regardless of KERNEL_FP_TYPE. */\n"
"__kernel void adam_update(__global FP_TYPE *param,       /* Parameter tensor (to be updated) */\n"
"                         __global const FP_TYPE *grad,       /* Gradient tensor dL/dparam */\n"
"                         __global float *m,           /* Adam state m (1st moment, float) */\n"
"                         __global float *v,           /* Adam state v (2nd moment, float) */\n"
"                         const int num_elements,   /* Total number of elements */\n"
"                         const float lr,             /* Learning rate */\n"
"                         const float beta1,          /* Adam beta1 hyperparameter */\n"
"                         const float beta2,          /* Adam beta2 hyperparameter */\n"
"                         const float epsilon,        /* Adam epsilon hyperparameter */\n"
"                         const float weight_decay,   /* Weight decay factor (L2 regularization) */\n"
"                         const float beta1_t,        /* Precomputed beta1^t */\n"
"                         const float beta2_t) {      /* Precomputed beta2^t */\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        /* Read values, using float for internal Adam calculations for stability/consistency */\n"
"        float p = (float)param[idx];\n"
"        float g = (float)grad[idx];\n"
"        float m_curr = m[idx]; /* Read current m state */\n"
"        float v_curr = v[idx]; /* Read current v state */\n"
"\n"
"        /* Apply weight decay (L2 regularization) if enabled */\n"
"        if (weight_decay > 0.0f) {\n"
"            g += weight_decay * p; /* Add weight decay term to the gradient */\n"
"        }\n"
"\n"
"        /* Update biased first moment estimate (m) */\n"
"        float m_new = beta1 * m_curr + (1.0f - beta1) * g;\n"
"        /* Update biased second raw moment estimate (v) */\n"
"        float v_new = beta2 * v_curr + (1.0f - beta2) * (g * g);\n"
"\n"
"        /* Compute bias-corrected first moment estimate (m_hat) */\n"
"        /* Add small epsilon to denominator for numerical stability, although 1-beta1_t is usually far from 0 early on. */\n"
"        float m_hat = m_new / (1.0f - beta1_t + 1e-9f);\n"
"        /* Compute bias-corrected second raw moment estimate (v_hat) */\n"
"        float v_hat = v_new / (1.0f - beta2_t + 1e-9f);\n"
"\n"
"        /* Compute the parameter update step */\n"
"        /* update = lr * m_hat / (sqrt(v_hat) + epsilon) */\n"
"        float update = lr * m_hat / (native_sqrt(v_hat) + epsilon);\n"
"\n"
"        /* Apply the update to the parameter */\n"
"        float p_new = p - update;\n"
"\n"
"        /* Write back updated parameter and Adam states */\n"
"        param[idx] = (FP_TYPE)p_new; /* Cast back to original parameter type */\n"
"        m[idx] = m_new;             /* Write updated m state (float) */\n"
"        v[idx] = v_new;             /* Write updated v state (float) */\n"
"    }\n"
"}";
// Embedding Lookup (GPU Version)
const char *embedding_lookup_kernel_src =
"/* Performs embedding lookup: output[b, s, :] = weights[indices[b, s], :] */\n"
"__kernel void embedding_lookup(\n"
"             __global const int* indices,     /* Input: Indices tensor (B, S) flattened (B*S,) */\n"
"             __global const FP_TYPE* weights, /* Input: Weight matrix (V, D) */\n"
"             __global FP_TYPE* output,        /* Output: Output tensor (B, S, D) flattened (B*S, D) */\n"
"             const int seq_len,     /* S */\n"
"             const int embed_dim,   /* D */\n"
"             const int vocab_size   /* V */\n"
"             /* B is implicit via global size dim 1 */\n"
"             ) {\n"
"    /* Use 2D global IDs mapping to (s, b) */\n"
"    int s = get_global_id(0); /* Sequence dimension index (0 to S-1) */\n"
"    int b = get_global_id(1); /* Batch dimension index (0 to B-1) */\n"
"\n"
"    /* Calculate linear index for the input indices array (B*S,) */\n"
"    size_t indices_idx = (size_t)b * seq_len + s;\n"
"\n"
"    /* Read the vocabulary index for this (b, s) position */\n"
"    int vocab_idx = indices[indices_idx];\n"
"\n"
"    /* Calculate the base offset for the output tensor row (B*S, D) */\n"
"    size_t output_offset = ((size_t)b * seq_len + s) * embed_dim;\n"
"\n"
"    /* --- Bounds Check for Vocabulary Index --- */\n"
"    /* Check if the vocabulary index is valid (within [0, vocab_size-1]) */\n"
"    if (vocab_idx < 0 || vocab_idx >= vocab_size) {\n"
"        /* Handle out-of-bounds index (e.g., padding or error) -> Output zeros */\n"
"        for(int d = 0; d < embed_dim; ++d) {\n"
"            output[output_offset + d] = (FP_TYPE)0.0;\n"
"        }\n"
"        return; /* Exit early for this work-item */\n"
"    }\n"
"    /* ----------------------------------------- */\n"
"\n"
"    /* Calculate the base offset for the corresponding row in the weight matrix (V, D) */\n"
"    size_t weight_offset = (size_t)vocab_idx * embed_dim;\n"
"\n"
"    /* Copy the embedding vector from weights to output for the full embedding dimension D */\n"
"    for (int d = 0; d < embed_dim; ++d) {\n"
"        output[output_offset + d] = weights[weight_offset + d];\n"
"    }\n"
"}";
// Embedding Backward Pass 1 Kernel (Local Reduction, No Atomics)
const char *embedding_backward_calc_delta_local_kernel_src =
"/* Define work-group size for reduction (can be tuned) */\n"
"#ifndef REDUCE_WG_SIZE\n"
"#define REDUCE_WG_SIZE 256\n"
"#endif\n"
"\n"
"/* Define accumulation type based on FP64 support */\n"
"#ifdef CL_HAS_FP64\n"
"    typedef double REDUCE_ACCUM_TYPE;\n"
"    #define REDUCE_ACCUM_CONST(x) (double)(x)\n"
"#else\n"
"    typedef float REDUCE_ACCUM_TYPE;\n"
"    #define REDUCE_ACCUM_CONST(x) (float)(x)\n"
"#endif\n"
"\n"
"__kernel void embedding_backward_calc_delta_local(\n"
"                 __global const FP_TYPE* grad_output, /* Input: Gradient dL/dOutput (B, S, D) flattened (B*S, D) */\n"
"                 __global const int* indices,         /* Input: Indices used in forward (B, S) flattened (B*S,) */\n"
"                 __global FP_TYPE* delta_dw,          /* Output: Temporary Delta Gradient (V, D), zero-initialized */\n"
"                 const int B_dim,      /* Batch size B */\n"
"                 const int S_dim,      /* Sequence length S */\n"
"                 const int D_dim,      /* Embedding dimension D */\n"
"                 const int V_dim,      /* Vocabulary size V */\n"
"                 __local REDUCE_ACCUM_TYPE* local_sums /* Local memory buffer, size = REDUCE_WG_SIZE */\n"
"                 ) {\n"
"\n"
"    /* --- Work-item / Work-group IDs --- */\n"
"    /* Each work-group computes one element delta_dw[v_out, d_out] */\n"
"    /* Kernel is launched with 1D GWS = V * D (number of groups) * LWS */\n"
"    size_t group_id = get_group_id(0); /* Group ID maps conceptually to output element index (0 to V*D - 1) */\n"
"    int tid = get_local_id(0);       /* Local thread ID within the work-group (0 to WGS-1) */\n"
"    int wg_size = get_local_size(0); /* Work-group size (REDUCE_WG_SIZE) */\n"
"\n"
"    /* Decompose linear group ID into the target vocabulary (v) and dimension (d) indices */\n"
"    int v_out = group_id / D_dim;\n"
"    int d_out = group_id % D_dim;\n"
"\n"
"    /* --- Bounds Check for the Group --- */\n"
"    if (v_out >= V_dim || d_out >= D_dim) {\n"
"        local_sums[tid] = REDUCE_ACCUM_CONST(0.0);\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"        return;\n"
"    }\n"
"\n"
"    /* Total number of (b, s) pairs to potentially check */\n"
"    size_t items_to_reduce = (size_t)B_dim * S_dim;\n"
"    /* Accumulator for this thread's partial sum */\n"
"    REDUCE_ACCUM_TYPE thread_sum = REDUCE_ACCUM_CONST(0.0);\n"
"\n"
"    /* --- Grid-Stride Loop for Initial Summation --- */\n"
"    for (size_t i = tid; i < items_to_reduce; i += wg_size) {\n"
"        int b = i / S_dim;\n"
"        int s = i % S_dim;\n"
"        size_t indices_idx = (size_t)b * S_dim + s;\n"
"        int current_vocab_idx = indices[indices_idx];\n"
"\n"
"        /* --- Check if this (b, s) contributes to the target v_out --- */\n"
"        if (current_vocab_idx == v_out) {\n"
"            size_t grad_output_idx = ((size_t)b * S_dim + s) * D_dim + d_out;\n"
"            thread_sum += (REDUCE_ACCUM_TYPE)grad_output[grad_output_idx];\n"
"        }\n"
"    } /* End of loop over items_to_reduce */\n"
"\n"
"    local_sums[tid] = thread_sum;\n"
"\n"
"    /* --- Work-Group Reduction using Local Memory --- */\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = wg_size / 2; offset > 0; offset /= 2) {\n"
"        if (tid < offset) {\n"
"            local_sums[tid] += local_sums[tid + offset];\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"\n"
"    /* --- Write Final Result --- */\n"
"    if (tid == 0) {\n"
"        size_t delta_dw_idx = (size_t)v_out * D_dim + d_out;\n"
"        delta_dw[delta_dw_idx] = (FP_TYPE)local_sums[0];\n"
"    }\n"
"}";
// Reduce Sum (Axis 0 and 1 for Bias Gradient)
const char *reduce_sum_kernel_src =
"/* Enable extensions if needed for local memory atomics (though not used here) */\n"
"#pragma OPENCL EXTENSION cl_khr_local_int32_base_atomics : enable\n"
"#pragma OPENCL EXTENSION cl_khr_global_int32_base_atomics : enable\n"
"#pragma OPENCL EXTENSION cl_khr_fp64 : enable /* For ACCUM_TYPE if needed */\n"
"\n"
"/* Define work-group size for reduction (can be tuned) */\n"
"#ifndef WORK_GROUP_SIZE_REDUCE\n"
"#define WORK_GROUP_SIZE_REDUCE 256\n"
"#endif\n"
"\n"
"#ifdef CL_HAS_FP64\n"
"    typedef double REDUCE_ACCUM_TYPE;\n"
"    #define REDUCE_ACCUM_CONST(x) (double)(x)\n"
"#else\n"
"    typedef float REDUCE_ACCUM_TYPE;\n"
"    #define REDUCE_ACCUM_CONST(x) (float)(x)\n"
"#endif\n"
"\n"
"/* Performs reduction sum over axes 0 (B) and 1 (M) of a 3D tensor (B, M, N). */\n"
"/* Output is a 1D tensor of size N. */\n"
"/* Uses local memory for efficient work-group reduction. */\n"
"__kernel void reduce_sum_axis01(\n"
"                __global const FP_TYPE* input, /* Input tensor (B, M, N) */\n"
"                __global FP_TYPE* output,      /* Output tensor (N) */\n"
"                const int B, const int M, const int N,\n"
"                __local REDUCE_ACCUM_TYPE* local_sums    /* Local memory buffer, size = WORK_GROUP_SIZE_REDUCE */\n"
"                ) {\n"
"    /* --- Work-item / Work-group IDs --- */\n"
"    int n_out_idx = get_group_id(0); /* Index for the output element N this group calculates (0 to N-1) */\n"
"    int tid = get_local_id(0);       /* Local thread ID within the work-group (0 to WGS-1) */\n"
"    int wg_size = get_local_size(0); /* Work-group size (WORK_GROUP_SIZE_REDUCE) */\n"
"\n"
"    /* Total number of elements to sum over per output element n_out_idx (B * M) */\n"
"    size_t items_to_reduce = (size_t)B * M;\n"
"    /* Accumulator for this thread's partial sum */\n"
"    REDUCE_ACCUM_TYPE thread_sum = REDUCE_ACCUM_CONST(0.0);\n"
"\n"
"    /* --- Grid-Stride Loop for Initial Summation --- */\n"
"    if (n_out_idx < N) { /* Ensure the group works on a valid output index */\n"
"        for (size_t i = tid; i < items_to_reduce; i += wg_size) {\n"
"            int b = i / M;\n"
"            int m = i % M;\n"
"            size_t input_idx = (size_t)b * M * N + (size_t)m * N + n_out_idx;\n"
"            thread_sum += (REDUCE_ACCUM_TYPE)input[input_idx];\n"
"        }\n"
"    }\n"
"    local_sums[tid] = thread_sum;\n"
"\n"
"    /* --- Work-Group Reduction using Local Memory --- */\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = wg_size / 2; offset > 0; offset /= 2) {\n"
"        if (tid < offset) { /* Only threads in the first half of the current range add */\n"
"            local_sums[tid] += local_sums[tid + offset];\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"\n"
"    /* --- Write Final Result --- */\n"
"    if (tid == 0 && n_out_idx < N) { /* Check group index validity again before writing */\n"
"        output[n_out_idx] = (FP_TYPE)local_sums[0]; /* Cast back to output type */\n"
"    }\n"
"}";
// Broadcast Add (General Bias Vector - 3D + 1D)
const char *broadcast_add_kernel_src =
"/* Performs broadcast addition: C[b, m, n] = A[b, m, n] + B_bias[n] */\n"
"__kernel void broadcast_add_bias(\n"
"                __global const FP_TYPE* a,     /* Input tensor A (B, M, N) */\n"
"                __global const FP_TYPE* b_bias,/* Input bias vector B (N) */\n"
"                __global FP_TYPE* c,           /* Output tensor C (B, M, N) */\n"
"                const int M, const int N      /* Dimensions M and N (B is implicit from GWS dim 2) */\n"
"                ) {\n"
"    int n = get_global_id(0); /* Index along dimension N (0 to N-1) */\n"
"    int m = get_global_id(1); /* Index along dimension M (0 to M-1) */\n"
"    int b = get_global_id(2); /* Index along dimension B (0 to B-1) */\n"
"\n"
"    if (n < N && m < M) {\n"
"       size_t idx_a_c = (size_t)b * M * N + (size_t)m * N + n;\n"
"       int idx_b = n;\n"
"       c[idx_a_c] = a[idx_a_c] + b_bias[idx_b];\n"
"    }\n"
"}";
// Bias Addition Kernel (Matrix[M, N] + Vector[N])
const char *add_bias_mn_kernel_src =
"/* Performs broadcast addition: C[m, n] = A[m, n] + B_bias[n] */\n"
"/* Assumes A and C have shape (M, N), B_bias has shape (N) */\n"
"__kernel void add_bias_mn(\n"
"                __global const FP_TYPE* a,     /* Input tensor A (M, N) */\n"
"                __global const FP_TYPE* b_bias,/* Input bias vector B (N) */\n"
"                __global FP_TYPE* c,           /* Output tensor C (M, N) */\n"
"                const int M, const int N      /* Dimensions M and N */\n"
"                ) {\n"
"    int n = get_global_id(0); /* Index along dimension N (0 to N-1) */\n"
"    int m = get_global_id(1); /* Index along dimension M (0 to M-1) */\n"
"\n"
"    if (n < N && m < M) {\n"
"       size_t idx_ac = (size_t)m * N + n;\n"
"       int idx_b = n;\n"
"       c[idx_ac] = a[idx_ac] + b_bias[idx_b];\n"
"    }\n"
"}";
// Transpose Last Two Dimensions (Batched)
const char *transpose_batched_kernel_src =
"/* Transposes the last two dimensions of a tensor: (..., D1, D2) -> (..., D2, D1) */\n"
"__kernel void transpose_batched_last_two(\n"
"                __global const FP_TYPE* input, /* Input tensor (..., D1, D2) */\n"
"                __global FP_TYPE* output,      /* Output tensor (..., D2, D1) */\n"
"                const int Dim1,           /* Size of the dimension originally at -2 */\n"
"                const int Dim2            /* Size of the dimension originally at -1 */\n"
"                /* Leading dimensions (...) are flattened into GWS dim 2 (b_linear) */\n"
"                ) {\n"
"    int d1_out = get_global_id(0); /* Index along the new Dim1 (output dim -2, size Dim2) */\n"
"    int d2_out = get_global_id(1); /* Index along the new Dim2 (output dim -1, size Dim1) */\n"
"    int b_linear = get_global_id(2); /* Linearized index for the leading batch dimensions */\n"
"\n"
"    int d1_in = d2_out; /* Input dim1 index maps from output dim2 index */\n"
"    int d2_in = d1_out; /* Input dim2 index maps from output dim1 index */\n"
"\n"
"    if (d1_out < Dim2 && d2_out < Dim1) {\n"
"        size_t slice_stride = (size_t)Dim1 * Dim2;\n"
"        size_t batch_offset = (size_t)b_linear * slice_stride;\n"
"        size_t input_idx  = batch_offset + (size_t)d1_in * Dim2 + d2_in;\n"
"        size_t output_idx = batch_offset + (size_t)d1_out * Dim1 + d2_out; /* Stride is Dim1 now */\n"
"        output[output_idx] = input[input_idx];\n"
"    }\n"
"}";
// Transpose Dimensions 1 and 2 (Batched, 4D)
const char *transpose_12_batched_kernel_src =
"/* Transposes dimensions 1 and 2 of a 4D tensor: (B, D1, D2, D3) -> (B, D2, D1, D3) */\n"
"__kernel void transpose_12_batched(\n"
"                __global const FP_TYPE* input,  /* Input tensor (B, D1, D2, D3) */\n"
"                __global FP_TYPE* output, /* Output tensor (B, D2, D1, D3) */\n"
"                const int B, const int D1, const int D2, const int D3\n"
"                ) {\n"
"    int d3_idx = get_global_id(0);\n"
"    int d1_out_idx = get_global_id(1);\n"
"    int d2_b_linear = get_global_id(2);\n"
"\n"
"    int d2_out_idx = d2_b_linear % D2;\n"
"    int b_idx      = d2_b_linear / D2;\n"
"\n"
"    if (b_idx < B && d1_out_idx < D1 && d2_out_idx < D2 && d3_idx < D3) {\n"
"         int d1_in_idx = d1_out_idx;\n"
"         int d2_in_idx = d2_out_idx;\n"
"         size_t input_idx = (size_t)b_idx * D1 * D2 * D3 + \n"
"                           (size_t)d1_in_idx * D2 * D3 +  \n"
"                           (size_t)d2_in_idx * D3 +       \n"
"                           d3_idx;\n"
"         size_t output_idx = (size_t)b_idx * D2 * D1 * D3 + \n"
"                            (size_t)d2_out_idx * D1 * D3 + \n"
"                            (size_t)d1_out_idx * D3 +    \n"
"                            d3_idx;\n"
"         output[output_idx] = input[input_idx];\n"
"    }\n"
"}";
// Matmul (Batched, 3D @ 3D)
const char *matmul_batched_kernel_src =
"/* Performs batched matrix multiplication: C[b,:,:] = A[b,:,:] @ B[b,:,:] */\n"
"__kernel void matmul_batched(__global const FP_TYPE *a, /* Input A (B, M, K) */\n"
"                           __global const FP_TYPE *b, /* Input B (B, K, N) */\n"
"                           __global FP_TYPE *c, /* Output C (B, M, N) */\n"
"                           const int B, const int M, const int N, const int K) {\n"
"    int col = get_global_id(0); /* Index along N dimension (0 to N-1) */\n"
"    int row = get_global_id(1); /* Index along M dimension (0 to M-1) */\n"
"    int batch_idx = get_global_id(2); /* Index along B dimension (0 to B-1) */\n"
"\n"
"    if (batch_idx < B && row < M && col < N) {\n"
"        float sum = 0.0f;\n"
"        size_t a_batch_offset = (size_t)batch_idx * M * K;\n"
"        size_t b_batch_offset = (size_t)batch_idx * K * N;\n"
"        size_t c_batch_offset = (size_t)batch_idx * M * N;\n"
"\n"
"        for (int k = 0; k < K; ++k) {\n"
"             sum += (float)a[a_batch_offset + row * K + k] * (float)b[b_batch_offset + k * N + col];\n"
"        }\n"
"        c[c_batch_offset + row * N + col] = (FP_TYPE)sum;\n"
"    }\n"
"}";
// Matmul Backward dA (Batched)
const char *matmul_batched_backward_dA_kernel_src =
"/* dA[b,m,k] = sum_n dC[b,m,n] * B[b,k,n] (equivalent to dC @ B^T, batched) */\n"
"__kernel void matmul_batched_backward_da(__global const FP_TYPE *dC, /* Gradient dC (B, M, N) */\n"
"                                       __global const FP_TYPE *B,  /* Original Input B (B, K, N) */\n"
"                                       __global FP_TYPE *dA, /* Output Gradient dA (B, M, K) */\n"
"                                       const int B_dim, const int M_dim, const int N_dim, const int K_dim) {\n"
"    int k = get_global_id(0); /* Index along K dimension (0 to K_dim-1) */\n"
"    int m = get_global_id(1); /* Index along M dimension (0 to M_dim-1) */\n"
"    int b = get_global_id(2); /* Index along B dimension (0 to B_dim-1) */\n"
"\n"
"    if (b < B_dim && m < M_dim && k < K_dim) {\n"
"        float gradient_sum = 0.0f;\n"
"        size_t dc_batch_offset = (size_t)b * M_dim * N_dim;\n"
"        size_t b_batch_offset  = (size_t)b * K_dim * N_dim;\n"
"        size_t da_batch_offset = (size_t)b * M_dim * K_dim;\n"
"\n"
"        for (int n = 0; n < N_dim; ++n) {\n"
"            gradient_sum += (float)dC[dc_batch_offset + m * N_dim + n] * (float)B[b_batch_offset + k * N_dim + n];\n"
"        }\n"
"        dA[da_batch_offset + m * K_dim + k] = (FP_TYPE)gradient_sum;\n"
"    }\n"
"}";
// Matmul Backward dB (Batched)
const char *matmul_batched_backward_dB_kernel_src =
"/* dB[b,k,n] = sum_m A[b,m,k] * dC[b,m,n] (equivalent to A^T @ dC, batched) */\n"
"__kernel void matmul_batched_backward_db(__global const FP_TYPE *A,  /* Original Input A (B, M, K) */\n"
"                                       __global const FP_TYPE *dC, /* Gradient dC (B, M, N) */\n"
"                                       __global FP_TYPE *dB, /* Output Gradient dB (B, K, N) */\n"
"                                       const int B_dim, const int M_dim, const int N_dim, const int K_dim) {\n"
"    int n = get_global_id(0); /* Index along N dimension (0 to N_dim-1) */\n"
"    int k = get_global_id(1); /* Index along K dimension (0 to K_dim-1) */\n"
"    int b = get_global_id(2); /* Index along B dimension (0 to B_dim-1) */\n"
"\n"
"    if (b < B_dim && k < K_dim && n < N_dim) {\n"
"        float gradient_sum = 0.0f;\n"
"        size_t a_batch_offset  = (size_t)b * M_dim * K_dim;\n"
"        size_t dc_batch_offset = (size_t)b * M_dim * N_dim;\n"
"        size_t db_batch_offset = (size_t)b * K_dim * N_dim;\n"
"\n"
"        for (int m = 0; m < M_dim; ++m) {\n"
"            gradient_sum += (float)A[a_batch_offset + m * K_dim + k] * (float)dC[dc_batch_offset + m * N_dim + n];\n"
"        }\n"
"        dB[db_batch_offset + k * N_dim + n] = (FP_TYPE)gradient_sum;\n"
"    }\n"
"}";
// Broadcast Add for Positional Encoding
const char *add_broadcast_pe_kernel_src =
"/* Performs broadcast addition: Output[b, s, e] = Input[b, s, e] + PE[s, e] */\n"
"__kernel void add_broadcast_pe(\n"
"                __global const FP_TYPE* input,  /* Input tensor (B, S, E) */\n"
"                __global const FP_TYPE* pe,     /* Positional Encoding tensor (S, E) - Slice matching S */\n"
"                __global FP_TYPE* output, /* Output tensor (B, S, E) */\n"
"                const int S, const int E        /* Dimensions S and E (B is implicit from GWS dim 2) */\n"
"                ) {\n"
"    int e = get_global_id(0); /* Index along dimension E (0 to E-1) */\n"
"    int s = get_global_id(1); /* Index along dimension S (0 to S-1) */\n"
"    int b = get_global_id(2); /* Index along dimension B (0 to B-1) */\n"
"\n"
"    if (s < S && e < E) {\n"
"       size_t idx_bse = (size_t)b * S * E + (size_t)s * E + e;\n"
"       size_t idx_pe = (size_t)s * E + e;\n"
"       output[idx_bse] = input[idx_bse] + pe[idx_pe];\n"
"    }\n"
"}";
// Hebbian Update (Local Reduction, No Atomics)
const char *hebbian_update_local_reduce_kernel_src =
"/* Define work-group size for reduction (can be tuned) */\n"
"#ifndef REDUCE_WG_SIZE\n"
"#define REDUCE_WG_SIZE 256\n"
"#endif\n"
"\n"
"/* Define accumulation type based on FP64 support */\n"
"#ifdef CL_HAS_FP64\n"
"    typedef double REDUCE_ACCUM_TYPE;\n"
"    #define REDUCE_ACCUM_CONST(x) (double)(x)\n"
"#else\n"
"    typedef float REDUCE_ACCUM_TYPE;\n"
"    #define REDUCE_ACCUM_CONST(x) (float)(x)\n"
"#endif\n"
"\n"
"__kernel void hebbian_update_local_reduce(\n"
"                                __global const FP_TYPE *A,  /* Pre-synaptic activations (B, M, K) */\n"
"                                __global const FP_TYPE *C,  /* Post-synaptic activations (B, M, N) */\n"
"                                __global FP_TYPE *W,        /* Weights to be updated (K, N) */\n"
"                                const float learning_rate,\n"
"                                const int B_dim, const int M_dim, const int N_dim, const int K_dim,\n"
"                                __local REDUCE_ACCUM_TYPE* local_sums /* Local memory buffer, size = REDUCE_WG_SIZE */\n"
"                                ) {\n"
"    size_t group_id = get_group_id(0);\n"
"    int tid = get_local_id(0);\n"
"    int wg_size = get_local_size(0);\n"
"\n"
"    int k_out = group_id / N_dim;\n"
"    int n_out = group_id % N_dim;\n"
"\n"
"    if (k_out >= K_dim || n_out >= N_dim) {\n"
"        local_sums[tid] = REDUCE_ACCUM_CONST(0.0);\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"        return;\n"
"    }\n"
"\n"
"    size_t items_to_reduce = (size_t)B_dim * M_dim;\n"
"    REDUCE_ACCUM_TYPE thread_sum = REDUCE_ACCUM_CONST(0.0);\n"
"\n"
"    for (size_t i = tid; i < items_to_reduce; i += wg_size) {\n"
"        int b = i / M_dim;\n"
"        int m = i % M_dim;\n"
"        size_t a_idx = (size_t)b * M_dim * K_dim + (size_t)m * K_dim + k_out;\n"
"        size_t c_idx = (size_t)b * M_dim * N_dim + (size_t)m * N_dim + n_out;\n"
"        thread_sum += (REDUCE_ACCUM_TYPE)A[a_idx] * (REDUCE_ACCUM_TYPE)C[c_idx];\n"
"    }\n"
"\n"
"    local_sums[tid] = thread_sum;\n"
"\n"
"    barrier(CLK_LOCAL_MEM_FENCE);\n"
"    for (int offset = wg_size / 2; offset > 0; offset /= 2) {\n"
"        if (tid < offset) {\n"
"            local_sums[tid] += local_sums[tid + offset];\n"
"        }\n"
"        barrier(CLK_LOCAL_MEM_FENCE);\n"
"    }\n"
"\n"
"    if (tid == 0) {\n"
"        size_t w_idx = (size_t)k_out * N_dim + n_out;\n"
"        W[w_idx] += (FP_TYPE)(learning_rate * local_sums[0]);\n"
"    }\n"
"}";
// Threshold Spike Generation
const char *threshold_spike_kernel_src =
"__kernel void threshold_spike( __global const FP_TYPE *activations,\n"
"                               __global FP_TYPE *spikes, /* Output: 0.0f or 1.0f */\n"
"                               const float threshold,\n"
"                               const int num_elements) {\n"
"    int idx = get_global_id(0); /* Global element index */\n"
"\n"
"    if (idx < num_elements) {\n"
"        spikes[idx] = (activations[idx] > threshold) ? (FP_TYPE)1.0f : (FP_TYPE)0.0f;\n"
"    }\n"
"}";
// Dynamic Token Assignment: Find closest prototype (max dot product)
const char *dynamic_token_assign_kernel_src =
"#ifndef HUGE_VALF\n"
"#define HUGE_VALF (__builtin_huge_valf())\n"
"#endif\n"
"\n"
"/* Assigns each input activation vector to the index of the prototype with the highest dot product similarity. */\n"
"__kernel void dynamic_token_assignment(\n"
"                            __global const FP_TYPE *activations, /* Input activations (B, S, E) flattened */\n"
"                            __global const FP_TYPE *prototypes,  /* Token prototypes (T, E) flattened */\n"
"                            __global int *output_indices,      /* Output token indices (B, S) flattened */\n"
"                            const int S, /* Sequence length */\n"
"                            const int E, /* Embedding dimension */\n"
"                            const int T  /* Number of token prototypes */\n"
"                            /* B is implicit via GWS dim 1 */\n"
"                            ) {\n"
"    int s = get_global_id(0); /* Sequence dimension index (0 to S-1) */\n"
"    int b = get_global_id(1); /* Batch dimension index (0 to B-1) */\n"
"\n"
"    size_t activation_offset = ((size_t)b * S + s) * E; /* Offset for activations[b, s, :] */\n"
"    size_t output_idx = (size_t)b * S + s;              /* Offset for output_indices[b, s] */\n"
"\n"
"    float max_similarity = -HUGE_VALF;\n"
"    int best_token_idx = -1; /* Initialize with invalid index */\n"
"\n"
"    /* Iterate through all token prototypes */\n"
"    for (int t = 0; t < T; ++t) {\n"
"        size_t prototype_offset = (size_t)t * E; /* Offset for prototypes[t, :] */\n"
"        float current_similarity = 0.0f;\n"
"\n"
"        /* Compute dot product between activation and prototype */\n"
"        for (int e = 0; e < E; ++e) {\n"
"            current_similarity += activations[activation_offset + e] * prototypes[prototype_offset + e];\n"
"        }\n"
"\n"
"        /* Update best match if current similarity is higher */\n"
"        if (current_similarity > max_similarity) {\n"
"            max_similarity = current_similarity;\n"
"            best_token_idx = t;\n"
"        }\n"
"    }\n"
"\n"
"    /* Write the index of the best matching prototype */\n"
"    output_indices[output_idx] = best_token_idx;\n"
"}";
// Pairwise Similarity (Dot Product)
const char *pairwise_similarity_kernel_src =
"/* Computes the pairwise dot product similarity matrix for a set of state vectors. */\n"
"__kernel void pairwise_similarity_dot(\n"
"                            __global const FP_TYPE *states, /* Input state vectors (N, D) flattened */\n"
"                            __global FP_TYPE *similarity,   /* Output similarity matrix (N, N) flattened */\n"
"                            const int N, /* Number of state vectors */\n"
"                            const int D  /* Dimension of state vectors */\n"
"                            ) {\n"
"    int i = get_global_id(0); /* Row index for the similarity matrix (0 to N-1) */\n"
"    int j = get_global_id(1); /* Column index for the similarity matrix (0 to N-1) */\n"
"\n"
"    if (i < N && j < N) {\n"
"        size_t state_i_offset = (size_t)i * D;\n"
"        size_t state_j_offset = (size_t)j * D;\n"
"        size_t output_idx = (size_t)i * N + j;\n"
"\n"
"        float dot_product = 0.0f;\n"
"        for (int d = 0; d < D; ++d) {\n"
"            dot_product += states[state_i_offset + d] * states[state_j_offset + d];\n"
"        }\n"
"        similarity[output_idx] = (FP_TYPE)dot_product;\n"
"    }\n"
"}";

const char *fused_diffusion_kernel_src =
"// Definiert M_PI, falls noch nicht vorhanden\n"
"#ifndef M_PI\n"
"#define M_PI 3.14159265358979323846f\n"
"#endif\n"
"\n"
"// Einfacher und schneller Pseudo-Zufallszahlengenerator (Xorshift)\n"
"inline uint xorshift_rng(uint state) {\n"
"    state ^= state << 13;\n"
"    state ^= state >> 17;\n"
"    state ^= state << 5;\n"
"    return state;\n"
"}\n"
"\n"
"// Erzeugt eine normalverteilte Zufallszahl aus einer uniformen mittels Box-Muller-Transformation\n"
"inline float random_normal(uint *seed) {\n"
"    *seed = xorshift_rng(*seed);\n"
"    // Konvertiert den uint-Zustand in einen float im Bereich (0, 1]\n"
"    float u1 = (*seed) / 4294967296.0f + (1.0f / 8589934592.0f);\n"
"    \n"
"    *seed = xorshift_rng(*seed);\n"
"    float u2 = (*seed) / 4294967296.0f + (1.0f / 8589934592.0f);\n"
"    \n"
"    // Box-Muller-Transformation\n"
"    float mag = sqrt(-2.0f * log(u1));\n"
"    return mag * cos(2.0f * M_PI * u2);\n"
"}\n"
"\n"
"/* Führt den Fused-Diffusion-Schritt durch: O = (1-gamma)*X + gamma*(W@X) + noise*sigma */\n"
"__kernel void fused_diffusion(\n"
"    __global const FP_TYPE *X,  /* Input state X [B, N, D] */\n"
"    __global const FP_TYPE *W,  /* Weight matrix W [B, N, N] */\n"
"    __global FP_TYPE *O,        /* Output state O [B, N, D] */\n"
"    const int B, const int N, const int D,\n"
"    const FP_TYPE gamma,        /* Mischfaktor */\n"
"    const FP_TYPE sigma,        /* Rauschstärke */\n"
"    const uint base_seed) {     /* Seed für den Zufallszahlengenerator */\n"
"\n"
"    // Jeder Work-Item berechnet ein Element im Output-Tensor O\n"
"    int gid = get_global_id(0);\n"
"    int total = B * N * D;\n"
"    if (gid >= total) return;\n"
"\n"
"    // Dekodieren des globalen Index in Batch-, Knoten- und Dimensions-Indizes\n"
"    const int d = gid % D;\n"
"    const int idx_nd = gid / D;\n"
"    const int n = idx_nd % N;\n"
"    const int b = idx_nd / N;\n"
"\n"
"    // Berechne den gemischten Term (W @ X)\n"
"    FP_TYPE mix = (FP_TYPE)0;\n"
"    const size_t w_row_offset = ((size_t)b * N + n) * N;\n"
"    const size_t x_batch_offset = (size_t)b * N * D;\n"
"    for (int j = 0; j < N; ++j) {\n"
"        mix += W[w_row_offset + j] * X[x_batch_offset + (size_t)j * D + d];\n"
"    }\n"
"\n"
"    const size_t x_idx = (size_t)b * N * D + (size_t)n * D + d;\n"
"    const FP_TYPE self_val = X[x_idx];\n"
"\n"
"    // Erzeuge Rauschen\n"
"    uint seed = base_seed + gid;\n"
"    FP_TYPE noise = (sigma > 0.0f) ? random_normal(&seed) * sigma : 0.0f;\n"
"\n"
"    // Wende die finale Formel an\n"
"    FP_TYPE one_minus_gamma = 1.0f - gamma;\n"
"    O[x_idx] = one_minus_gamma * self_val + gamma * mix + noise;\n"
"}\n";

const char *conv2d_forward_kernel_src =
"__kernel void conv2d_forward(\n"
"    __global const FP_TYPE* input,\n"
"    __global const FP_TYPE* weights,\n"
"    __global const FP_TYPE* bias,\n"
"    __global FP_TYPE* output,\n"
"    const int B, const int C_in, const int H, const int W,\n"
"    const int C_out, const int K_h, const int K_w,\n"
"    const int stride_h, const int stride_w,\n"
"    const int out_h, const int out_w) {\n"
"    int gid = get_global_id(0);\n"
"    int total = B * C_out * out_h * out_w;\n"
"    if (gid >= total) { return; }\n"
"    int ow = gid % out_w;\n"
"    int tmp = gid / out_w;\n"
"    int oh = tmp % out_h;\n"
"    tmp /= out_h;\n"
"    int oc = tmp % C_out;\n"
"    int b = tmp / C_out;\n"
"    FP_TYPE acc = (bias ? bias[oc] : (FP_TYPE)0);\n"
"    for (int ic = 0; ic < C_in; ++ic) {\n"
"        for (int kh = 0; kh < K_h; ++kh) {\n"
"            int ih = oh * stride_h + kh;\n"
"            for (int kw = 0; kw < K_w; ++kw) {\n"
"                int iw = ow * stride_w + kw;\n"
"                size_t in_idx = (((size_t)b * C_in + ic) * H + ih) * W + iw;\n"
"                size_t w_idx = ((((size_t)oc * C_in) + ic) * K_h + kh) * K_w + kw;\n"
"                acc += weights[w_idx] * input[in_idx];\n"
"            }\n"
"        }\n"
"    }\n"
"    output[gid] = acc;\n"
"}\n";

const char *conv2d_backward_input_kernel_src =
"__kernel void conv2d_backward_input(\n"
"    __global const FP_TYPE* grad_output,\n"
"    __global const FP_TYPE* weights,\n"
"    __global FP_TYPE* grad_input,\n"
"    const int B, const int C_in, const int H, const int W,\n"
"    const int C_out, const int K_h, const int K_w,\n"
"    const int stride_h, const int stride_w,\n"
"    const int out_h, const int out_w) {\n"
"    int gid = get_global_id(0);\n"
"    int total = B * C_in * H * W;\n"
"    if (gid >= total) { return; }\n"
"    int iw = gid % W;\n"
"    int tmp = gid / W;\n"
"    int ih = tmp % H;\n"
"    tmp /= H;\n"
"    int ic = tmp % C_in;\n"
"    int b = tmp / C_in;\n"
"    FP_TYPE acc = (FP_TYPE)0;\n"
"    for (int oc = 0; oc < C_out; ++oc) {\n"
"        for (int oh = 0; oh < out_h; ++oh) {\n"
"            int kh = ih - oh * stride_h;\n"
"            if (kh < 0 || kh >= K_h) { continue; }\n"
"            for (int ow = 0; ow < out_w; ++ow) {\n"
"                int kw = iw - ow * stride_w;\n"
"                if (kw < 0 || kw >= K_w) { continue; }\n"
"                size_t go_idx = (((size_t)b * C_out + oc) * out_h + oh) * out_w + ow;\n"
"                size_t w_idx = ((((size_t)oc * C_in) + ic) * K_h + kh) * K_w + kw;\n"
"                acc += grad_output[go_idx] * weights[w_idx];\n"
"            }\n"
"        }\n"
"    }\n"
"    grad_input[gid] = acc;\n"
"}\n";

const char *conv2d_backward_weight_kernel_src =
"__kernel void conv2d_backward_weight(\n"
"    __global const FP_TYPE* grad_output,\n"
"    __global const FP_TYPE* input,\n"
"    __global FP_TYPE* grad_weight,\n"
"    const int B, const int C_in, const int H, const int W,\n"
"    const int C_out, const int K_h, const int K_w,\n"
"    const int stride_h, const int stride_w,\n"
"    const int out_h, const int out_w) {\n"
"    int gid = get_global_id(0);\n"
"    int total = C_out * C_in * K_h * K_w;\n"
"    if (gid >= total) { return; }\n"
"    int kw = gid % K_w;\n"
"    int tmp = gid / K_w;\n"
"    int kh = tmp % K_h;\n"
"    tmp /= K_h;\n"
"    int ic = tmp % C_in;\n"
"    int oc = tmp / C_in;\n"
"    FP_TYPE acc = (FP_TYPE)0;\n"
"    for (int b = 0; b < B; ++b) {\n"
"        for (int oh = 0; oh < out_h; ++oh) {\n"
"            int ih = oh * stride_h + kh;\n"
"            for (int ow = 0; ow < out_w; ++ow) {\n"
"                int iw = ow * stride_w + kw;\n"
"                size_t go_idx = (((size_t)b * C_out + oc) * out_h + oh) * out_w + ow;\n"
"                size_t in_idx = (((size_t)b * C_in + ic) * H + ih) * W + iw;\n"
"                acc += grad_output[go_idx] * input[in_idx];\n"
"            }\n"
"        }\n"
"    }\n"
"    grad_weight[gid] = acc;\n"
"}\n";

const char *conv2d_bias_grad_kernel_src =
"__kernel void conv2d_bias_grad(\n"
"    __global const FP_TYPE* grad_output,\n"
"    __global FP_TYPE* grad_bias,\n"
"    const int B, const int C_out, const int out_h, const int out_w) {\n"
"    int oc = get_global_id(0);\n"
"    if (oc >= C_out) { return; }\n"
"    FP_TYPE acc = (FP_TYPE)0;\n"
"    for (int b = 0; b < B; ++b) {\n"
"        for (int oh = 0; oh < out_h; ++oh) {\n"
"            for (int ow = 0; ow < out_w; ++ow) {\n"
"                size_t go_idx = (((size_t)b * C_out + oc) * out_h + oh) * out_w + ow;\n"
"                acc += grad_output[go_idx];\n"
"            }\n"
"        }\n"
"    }\n"
"    grad_bias[oc] = acc;\n"
"}\n";

const char *patch_permute_kernel_src =
"__kernel void patch_permute_reshape(\n"
"    __global const FP_TYPE* input,\n"
"    __global FP_TYPE* output,\n"
"    const int B, const int C, const int H, const int W) {\n"
"    int gid = get_global_id(0);\n"
"    int total = B * C * H * W;\n"
"    if (gid >= total) { return; }\n"
"    int w = gid % W;\n"
"    int tmp = gid / W;\n"
"    int h = tmp % H;\n"
"    tmp /= H;\n"
"    int c = tmp % C;\n"
"    int b = tmp / C;\n"
"    size_t in_idx = (((size_t)b * C + c) * H + h) * W + w;\n"
"    size_t out_idx = (((size_t)b * W + w) * H + h) * C + c;\n"
"    output[out_idx] = input[in_idx];\n"
"}\n";

const char *patch_permute_backward_kernel_src =
"__kernel void patch_permute_reshape_backward(\n"
"    __global const FP_TYPE* grad_tokens,\n"
"    __global FP_TYPE* grad_feature,\n"
"    const int B, const int C, const int H, const int W) {\n"
"    int gid = get_global_id(0);\n"
"    int total = B * C * H * W;\n"
"    if (gid >= total) { return; }\n"
"    int w = gid % W;\n"
"    int tmp = gid / W;\n"
"    int h = tmp % H;\n"
"    tmp /= H;\n"
"    int c = tmp % C;\n"
"    int b = tmp / C;\n"
"    size_t grad_idx = (((size_t)b * C + c) * H + h) * W + w;\n"
"    size_t token_idx = (((size_t)b * W + w) * H + h) * C + c;\n"
"    grad_feature[grad_idx] = grad_tokens[token_idx];\n"
"}\n";

const char *izhikevich_kernel_src =
"/* Simuliert einen Zeitschritt des Izhikevich-Neuronmodells mit Heun-Integration. */\n"
"__kernel void izhikevich_neuron_step(\n"
"    __global FP_TYPE *v,\n"
"    __global FP_TYPE *u,\n"
"    __global const FP_TYPE *i_inj,\n"
"    __global FP_TYPE *spikes_out,\n"
"    __global const FP_TYPE *p_a,\n"
"    __global const FP_TYPE *p_b,\n"
"    __global const FP_TYPE *p_c,\n"
"    __global const FP_TYPE *p_d,\n"
"    const FP_TYPE dt,\n"
"    const FP_TYPE threshold,\n"
"    const int num_neurons) {\n"
"    int gid = get_global_id(0);\n"
"    if (gid >= num_neurons) { return; }\n"
"    FP_TYPE v_local = v[gid];\n"
"    FP_TYPE u_local = u[gid];\n"
"    const FP_TYPE a = p_a[gid];\n"
"    const FP_TYPE b = p_b[gid];\n"
"    const FP_TYPE c = p_c[gid];\n"
"    const FP_TYPE d = p_d[gid];\n"
"    const FP_TYPE input = i_inj[gid];\n"
"    const FP_TYPE half_dt = dt * (FP_TYPE)0.5;\n"
"    for (int step = 0; step < 2; ++step) {\n"
"        FP_TYPE dv = (FP_TYPE)0.04 * v_local * v_local + (FP_TYPE)5.0 * v_local + (FP_TYPE)140.0 - u_local + input;\n"
"        FP_TYPE du = a * (b * v_local - u_local);\n"
"        v_local += half_dt * dv;\n"
"        u_local += half_dt * du;\n"
"    }\n"
"    FP_TYPE spiked = (v_local >= threshold) ? (FP_TYPE)1 : (FP_TYPE)0;\n"
"    if (spiked > (FP_TYPE)0) {\n"
"        v_local = c;\n"
"        u_local += d;\n"
"    }\n"
"    v[gid] = v_local;\n"
"    u[gid] = u_local;\n"
"    if (spikes_out) {\n"
"        spikes_out[gid] = spiked;\n"
"    }\n"
"}\n";

const char *stdp_update_kernel_src =
"/* Wendet STDP-Gewichtsaktualisierungen basierend auf Spike-Traces an. */\n"
"__kernel void stdp_update_step(\n"
"    __global FP_TYPE *weights,\n"
"    __global const FP_TYPE *pre_traces,\n"
"    __global const FP_TYPE *post_traces,\n"
"    __global const int *pre_spike_events,\n"
"    __global const int *post_spike_events,\n"
"    const FP_TYPE lr_ltp,\n"
"    const FP_TYPE lr_ltd,\n"
"    const int pre_n,\n"
"    const int post_n) {\n"
"    int gid = get_global_id(0);\n"
"    int total = pre_n * post_n;\n"
"    if (gid >= total) { return; }\n"
"    int pre_idx = gid / post_n;\n"
"    int post_idx = gid - pre_idx * post_n;\n"
"    FP_TYPE w = weights[gid];\n"
"    if (post_spike_events[post_idx] != 0) {\n"
"        w += lr_ltp * pre_traces[pre_idx];\n"
"    }\n"
"    if (pre_spike_events[pre_idx] != 0) {\n"
"        w -= lr_ltd * post_traces[post_idx];\n"
"    }\n"
"    weights[gid] = w;\n"
"}\n";

const char *stdp_trace_kernel_src =
"/* Aktualisiert exponentiell abfallende Spike-Traces für STDP. */\n"
"__kernel void stdp_update_traces(\n"
"    __global FP_TYPE *pre_traces,\n"
"    __global FP_TYPE *post_traces,\n"
"    __global const int *pre_spike_events,\n"
"    __global const int *post_spike_events,\n"
"    const FP_TYPE decay_pre,\n"
"    const FP_TYPE decay_post,\n"
"    const FP_TYPE increment_pre,\n"
"    const FP_TYPE increment_post,\n"
"    const int pre_n,\n"
"    const int post_n) {\n"
"    int gid = get_global_id(0);\n"
"    int max_n = pre_n > post_n ? pre_n : post_n;\n"
"    if (gid >= max_n) { return; }\n"
"    if (gid < pre_n) {\n"
"        FP_TYPE trace = pre_traces[gid] * decay_pre;\n"
"        if (pre_spike_events[gid] != 0) {\n"
"            trace += increment_pre;\n"
"        }\n"
"        pre_traces[gid] = trace;\n"
"    }\n"
"    if (gid < post_n) {\n"
"        FP_TYPE trace = post_traces[gid] * decay_post;\n"
"        if (post_spike_events[gid] != 0) {\n"
"            trace += increment_post;\n"
"        }\n"
"        post_traces[gid] = trace;\n"
"    }\n"
"}\n";

const char *lbm_kernel_src =
"/* Führt Kollision und Streaming für ein D2Q9-Lattice-Boltzmann-Gitter aus. */\n"
"__kernel void lbm_collide_and_stream(\n"
"    __global const FP_TYPE *f_in,\n"
"    __global FP_TYPE *f_out,\n"
"    __global FP_TYPE *rho,\n"
"    __global FP_TYPE *ux,\n"
"    __global FP_TYPE *uy,\n"
"    const FP_TYPE omega,\n"
"    const int width,\n"
"    const int height) {\n"
"    int gid = get_global_id(0);\n"
"    int total = width * height;\n"
"    if (gid >= total) { return; }\n"
"    int x = gid % width;\n"
"    int y = gid / width;\n"
"    int base = gid * 9;\n"
"    FP_TYPE fi[9];\n"
"    for (int i = 0; i < 9; ++i) { fi[i] = f_in[base + i]; }\n"
"    FP_TYPE rho_local = (FP_TYPE)0;\n"
"    for (int i = 0; i < 9; ++i) { rho_local += fi[i]; }\n"
"    FP_TYPE ux_local = (fi[1] - fi[3] + fi[5] - fi[6] - fi[7] + fi[8]) / rho_local;\n"
"    FP_TYPE uy_local = (fi[2] - fi[4] + fi[5] + fi[6] - fi[7] - fi[8]) / rho_local;\n"
"    rho[gid] = rho_local;\n"
"    ux[gid] = ux_local;\n"
"    uy[gid] = uy_local;\n"
"    FP_TYPE u2 = ux_local * ux_local + uy_local * uy_local;\n"
"    const FP_TYPE w[9] = {\n"
"        (FP_TYPE)(4.0f/9.0f),\n"
"        (FP_TYPE)(1.0f/9.0f), (FP_TYPE)(1.0f/9.0f), (FP_TYPE)(1.0f/9.0f), (FP_TYPE)(1.0f/9.0f),\n"
"        (FP_TYPE)(1.0f/36.0f), (FP_TYPE)(1.0f/36.0f), (FP_TYPE)(1.0f/36.0f), (FP_TYPE)(1.0f/36.0f)\n"
"    };\n"
"    const int cx[9] = {0, 1, 0, -1, 0, 1, -1, -1, 1};\n"
"    const int cy[9] = {0, 0, 1, 0, -1, 1, 1, -1, -1};\n"
"    FP_TYPE feq[9];\n"
"    for (int i = 0; i < 9; ++i) {\n"
"        FP_TYPE cu = (FP_TYPE)3.0 * (cx[i] * ux_local + cy[i] * uy_local);\n"
"        feq[i] = w[i] * rho_local * ((FP_TYPE)1 + cu + (FP_TYPE)0.5 * cu * cu - (FP_TYPE)1.5 * u2);\n"
"    }\n"
"    FP_TYPE post[9];\n"
"    for (int i = 0; i < 9; ++i) {\n"
"        post[i] = fi[i] - omega * (fi[i] - feq[i]);\n"
"    }\n"
"    for (int i = 0; i < 9; ++i) {\n"
"        int nx = (x + cx[i] + width) % width;\n"
"        int ny = (y + cy[i] + height) % height;\n"
"        int dest = (ny * width + nx) * 9 + i;\n"
"        f_out[dest] = post[i];\n"
"    }\n"
"}\n";

const char *nbody_forces_kernel_src =
"/* Berechnet Gravitationskräfte für jedes Teilchen im N-Körper-System. */\n"
"__kernel void nbody_calculate_forces(\n"
"    __global const float4 *positions,\n"
"    __global float4 *forces,\n"
"    const FP_TYPE gravitational_const,\n"
"    const FP_TYPE softening_factor,\n"
"    const int num_bodies) {\n"
"    int gid = get_global_id(0);\n"
"    if (gid >= num_bodies) { return; }\n"
"    float4 pos_i = positions[gid];\n"
"    float3 force = (float3)(0.0f, 0.0f, 0.0f);\n"
"    for (int j = 0; j < num_bodies; ++j) {\n"
"        float4 pos_j = positions[j];\n"
"        float3 r = (float3)(pos_j.x - pos_i.x, pos_j.y - pos_i.y, pos_j.z - pos_i.z);\n"
"        float dist2 = dot(r, r) + (float)softening_factor;\n"
"        if (dist2 > 0.0f) {\n"
"            float invDist = rsqrt(dist2);\n"
"            float invDist3 = invDist * invDist * invDist;\n"
"            float scale = (float)gravitational_const * pos_j.w * invDist3;\n"
"            force += r * scale;\n"
"        }\n"
"    }\n"
"    forces[gid] = (float4)(force.x, force.y, force.z, 0.0f);\n"
"}\n";

const char *nbody_integrate_kernel_src =
"/* Integriert Positionen und Geschwindigkeiten anhand der berechneten Kräfte. */\n"
"__kernel void nbody_integrate(\n"
"    __global float4 *positions,\n"
"    __global float4 *velocities,\n"
"    __global const float4 *forces,\n"
"    const FP_TYPE dt,\n"
"    const int num_bodies) {\n"
"    int gid = get_global_id(0);\n"
"    if (gid >= num_bodies) { return; }\n"
"    float4 pos = positions[gid];\n"
"    float4 vel = velocities[gid];\n"
"    float4 force = forces[gid];\n"
"    FP_TYPE mass = pos.w > (FP_TYPE)0 ? pos.w : (FP_TYPE)1;\n"
"    FP_TYPE inv_mass = (FP_TYPE)1 / mass;\n"
"    vel.x += force.x * (float)(inv_mass * dt);\n"
"    vel.y += force.y * (float)(inv_mass * dt);\n"
"    vel.z += force.z * (float)(inv_mass * dt);\n"
"    pos.x += vel.x * (float)dt;\n"
"    pos.y += vel.y * (float)dt;\n"
"    pos.z += vel.z * (float)dt;\n"
"    positions[gid] = pos;\n"
"    velocities[gid] = vel;\n"
"}\n";

const char *ising_kernel_src =
"/* Führt einen Checkerboard-Metropolis-Schritt für das 2D-Ising-Modell aus. */\n"
"__kernel void ising_metropolis_step(\n"
"    __global int *spin_grid,\n"
"    __global const FP_TYPE *random_numbers,\n"
"    const FP_TYPE J,\n"
"    const FP_TYPE beta,\n"
"    const int width,\n"
"    const int height,\n"
"    const int color) {\n"
"    int gid = get_global_id(0);\n"
"    int total = width * height;\n"
"    if (gid >= total) { return; }\n"
"    int x = gid % width;\n"
"    int y = gid / width;\n"
"    if (((x + y) & 1) != (color & 1)) { return; }\n"
"    int idx = y * width + x;\n"
"    int up = spin_grid[((y + 1) % height) * width + x];\n"
"    int down = spin_grid[((y - 1 + height) % height) * width + x];\n"
"    int left = spin_grid[y * width + ((x - 1 + width) % width)];\n"
"    int right = spin_grid[y * width + ((x + 1) % width)];\n"
"    int spin = spin_grid[idx];\n"
"    int neighbor_sum = up + down + left + right;\n"
"    FP_TYPE deltaE = (FP_TYPE)2 * J * (FP_TYPE)spin * (FP_TYPE)neighbor_sum;\n"
"    int rand_idx = idx >> 1;\n"
"    FP_TYPE rnd = random_numbers[rand_idx];\n"
"    if (deltaE <= (FP_TYPE)0 || rnd < exp(-beta * deltaE)) {\n"
"        spin_grid[idx] = -spin;\n"
"    }\n"
"}\n";
// GPU Prototype Update Kernel Sources
const char *proto_segmented_sum_atomic_kernel_src =
"/* This kernel requires the cl_khr_global_int32_base_atomics extension */\n"
"/* The CL_HAS_ATOMICS define MUST be passed by the host if the extension is supported */\n"
"#ifdef CL_HAS_ATOMICS\n"
"#pragma OPENCL EXTENSION cl_khr_global_int32_base_atomics : enable\n"
"/* Optionally enable 64-bit atomics when available for improved stability */\n"
"#ifdef CL_HAS_INT64_ATOMICS\n"
"#pragma OPENCL EXTENSION cl_khr_int64_base_atomics : enable\n"
"#pragma OPENCL EXTENSION cl_khr_int64_extended_atomics : enable\n"
"#endif\n"
"\n"
"/* Performs atomic add for floats using compare-and-swap (cmpxchg). */\n"
"inline void atomic_add_float(__global float *addr, float val) {\n"
"    union {\n"
"        unsigned int u32;\n"
"        float f32;\n"
"    } next, expected, current;\n"
"    /* Cast the float pointer to a global pointer to unsigned int for atom_cmpxchg */\n"
"    __global unsigned int *u_addr = (__global unsigned int *)addr;\n"
"    current.f32 = *addr; // Read current value non-atomically (initial guess)\n"
"    do {\n"
"        expected.f32 = current.f32; // Expected value for cmpxchg\n"
"        next.f32 = expected.f32 + val; // Calculate the desired new value\n"
"        // Atomically compare the value at u_addr with expected.u32.\n"
"        // If they match, replace the value at u_addr with next.u32.\n"
"        // Return the *original* value that was at u_addr before the attempt.\n"
"        current.u32 = atom_cmpxchg(u_addr, expected.u32, next.u32);\n"
"    } while (current.u32 != expected.u32); // Loop if the value was changed by another thread between read and write\n"
"}\n"
"\n"
"/* Sums activations belonging to the same prototype index atomically. */\n"
"__kernel void proto_segmented_sum_atomic(\n"
"        __global const FP_TYPE* activations_flat, /* Flattened activations (M_flat * E) */\n"
"        __global const int* indices_flat,       /* Flattened indices (M_flat) mapping activations to prototypes */\n"
"        __global FP_TYPE* proto_sums,           /* Output sums per prototype (T * E), MUST be zero-initialized by host */\n"
"        __global int* proto_counts,             /* Output counts per prototype (T), MUST be zero-initialized by host */\n"
"        const int M_flat, /* Total number of activation vectors (e.g., B * S) */\n"
"        const int E,      /* Embedding dimension */\n"
"        const int T       /* Number of prototypes */\n"
"        ) {\n"
"    int idx = get_global_id(0); // Global index iterates through all activation vectors\n"
"\n"
"    // Check if this work-item is within the bounds of the activation vectors\n"
"    if (idx < M_flat) {\n"
"        // Get the prototype index assigned to this activation vector\n"
"        int proto_idx = indices_flat[idx];\n"
"\n"
"        // Ensure the prototype index is valid\n"
"        if (proto_idx >= 0 && proto_idx < T) {\n"
"            // Atomically increment the counter for this prototype\n"
"            atom_inc(&proto_counts[proto_idx]);\n"
"\n"
"            // Calculate the offset for the current activation vector's data\n"
"            size_t activation_offset = (size_t)idx * E;\n"
"            // Calculate the base offset for the target prototype's sum data\n"
"            size_t sum_offset = (size_t)proto_idx * E;\n"
"\n"
"            // Iterate through the embedding dimension\n"
"            for (int e = 0; e < E; ++e) {\n"
"                // Atomically add the activation component to the corresponding prototype sum\n"
"                atomic_add_float(&proto_sums[sum_offset + e], activations_flat[activation_offset + e]);\n"
"            }\n"
"        }\n"
"        // Ignore activations assigned to invalid prototype indices (e.g., -1 for padding)\n"
"    }\n"
"}\n"
"#else\n"
"/* If atomics are NOT supported, provide a dummy kernel to avoid compile errors, */\n"
"/* but this kernel will do nothing. The host should prevent its execution. */\n"
"__kernel void proto_segmented_sum_atomic(\n"
"        __global const FP_TYPE* activations_flat,\n"
"        __global const int* indices_flat,\n"
"        __global FP_TYPE* proto_sums,\n"
"        __global int* proto_counts,\n"
"        const int M_flat, const int E, const int T) {\n"
"        /* Atomic operations not supported or enabled. This kernel does nothing. */\n"
"        /* Host code should have checked has_atomics_support before enqueuing. */\n"
"}\n"
"#endif\n";
const char *proto_update_step_kernel_src =
"/* Updates prototypes using the accumulated sums and counts */\n"
"__kernel void proto_update_step(\n"
"        __global FP_TYPE* prototypes,     /* Prototypes to be updated (T * E) */\n"
"        __global const FP_TYPE* proto_sums, /* Input sums per prototype (T * E) from segmented_sum */\n"
"        __global const int* proto_counts,   /* Input counts per prototype (T) from segmented_sum */\n"
"        const float learning_rate,        /* Learning rate (alpha) for the update */\n"
"        const int E,                      /* Embedding dimension */\n"
"        const int T                       /* Number of prototypes */\n"
"        ) {\n"
"    // Global ID corresponds to the prototype index\n"
"    int t = get_global_id(0);\n"
"\n"
"    // Check if this work-item is within the bounds of the prototypes\n"
"    if (t < T) {\n"
"        // Get the number of activations assigned to this prototype\n"
"        int count = proto_counts[t];\n"
"\n"
"        // Only update prototypes that received at least one activation vector\n"
"        if (count > 0) {\n"
"            // Calculate base offset for this prototype's data\n"
"            size_t base_offset = (size_t)t * E;\n"
"            // Calculate inverse count for averaging\n"
"            float inv_count = 1.0f / (float)count;\n"
"            // Precompute learning rate factors\n"
"            float lr = learning_rate;\n"
"            float one_minus_lr = 1.0f - lr;\n"
"\n"
"            // Iterate through the embedding dimension\n"
"            for (int e = 0; e < E; ++e) {\n"
"                // Calculate the index for the current dimension\n"
"                size_t current_idx = base_offset + e;\n"
"                // Read the current prototype value\n"
"                float old_proto = prototypes[current_idx];\n"
"                // Calculate the mean activation value for this dimension\n"
"                float mean_activation = proto_sums[current_idx] * inv_count;\n"
"                // Apply the exponential moving average update rule:\n"
"                // new_proto = (1 - lr) * old_proto + lr * mean_activation\n"
"                prototypes[current_idx] = one_minus_lr * old_proto + lr * mean_activation;\n"
"            }\n"
"        }\n"
"        // Prototypes with count == 0 remain unchanged.\n"
"    }\n"
"}";
// Loss Shaping Kernel (Single Pair - Original)
const char *shape_loss_reward_penalty_kernel_src =
"/* Applies reward/penalty adjustments to pre-calculated loss values. */\n"
"/* Assumes 'predictions' buffer contains probabilities (output of softmax). */\n"
"__kernel void shape_loss_reward_penalty(\n"
"        __global const FP_TYPE* loss_in,           /* Input: Original loss per sample (num_samples) */\n"
"        __global const FP_TYPE* predictions,       /* Input: Model prediction probabilities (num_samples, num_classes) */\n"
"        __global const int* targets,             /* Input: Target class indices (num_samples) */\n"
"        __global FP_TYPE* loss_out,          /* Output: Shaped loss per sample (num_samples) */\n"
"        const int num_samples,             /* Total number of samples/tokens */\n"
"        const int num_classes,             /* Number of output classes (V) */\n"
"        const float penalty_weight,        /* Amount to ADD to loss for critical error */\n"
"        const float reward_weight,         /* Amount to SUBTRACT from loss for high-confidence correct prediction */\n"
"        const float high_confidence_threshold, /* Probability threshold for reward */\n"
"        const int critical_target_class,   /* Target class index for penalty check */\n"
"        const int critical_predicted_class /* Predicted class index for penalty check */\n"
"        )\n"
"{\n"
"    int idx = get_global_id(0); /* Global index for the sample/token */\n"
"\n"
"    if (idx < num_samples)\n"
"    {\n"
"        FP_TYPE current_loss = loss_in[idx];\n"
"        int target_label = targets[idx];\n"
"\n"
"        /* Handle padding/invalid target labels: Do not apply reward/penalty */\n"
"        if (target_label < 0 || target_label >= num_classes) {\n"
"            loss_out[idx] = current_loss;\n"
"            return;\n"
"        }\n"
"\n"
"        /* Find predicted class and its probability, and probability of correct class */\n"
"        size_t pred_offset = (size_t)idx * num_classes;\n"
"        int predicted_label = 0;\n"
"        FP_TYPE max_prob = -1.0f;\n"
"        for (int v = 0; v < num_classes; ++v) {\n"
"            FP_TYPE prob = predictions[pred_offset + v];\n"
"            if (prob > max_prob) {\n"
"                max_prob = prob;\n"
"                predicted_label = v;\n"
"            }\n"
"        }\n"
"        FP_TYPE correct_class_prob = predictions[pred_offset + target_label];\n"
"\n"
"        /* Calculate adjustment */\n"
"        float adjustment = 0.0f;\n"
"\n"
"        /* Penalty Logic */\n"
"        bool is_critical_error = (target_label == critical_target_class) && (predicted_label == critical_predicted_class);\n"
"        if (is_critical_error) {\n"
"            adjustment += penalty_weight;\n"
"        }\n"
"\n"
"        /* Reward Logic */\n"
"        bool is_correct = (predicted_label == target_label);\n"
"        bool is_high_confidence = (correct_class_prob >= high_confidence_threshold);\n"
"        if (is_correct && is_high_confidence) {\n"
"            adjustment -= reward_weight;\n"
"        }\n"
"\n"
"        /* Apply adjustment to the original loss */\n"
"        loss_out[idx] = current_loss + (FP_TYPE)adjustment;\n"
"    }\n"
"}";

// --- NEU: Loss Shaping Kernel (mit Liste) ---
const char *shape_loss_reward_penalty_list_kernel_src =
"/* Applies reward/penalty adjustments based on a list of critical pairs. */\n"
"/* Assumes 'predictions' buffer contains probabilities (output of softmax). */\n"
"__kernel void shape_loss_reward_penalty_list(\n"
"        __global const FP_TYPE* loss_in,           /* Input: Original loss per sample (num_samples) */\n"
"        __global const FP_TYPE* predictions,       /* Input: Model prediction probabilities (num_samples, num_classes) */\n"
"        __global const int* targets,             /* Input: Target class indices (num_samples) */\n"
"        __global FP_TYPE* loss_out,          /* Output: Shaped loss per sample (num_samples) */\n"
"        __global const int* critical_pairs,      /* Input: List of [target_id, predicted_id] pairs flattened (num_critical_pairs * 2) */\n"
"        const int num_samples,             /* Total number of samples/tokens */\n"
"        const int num_classes,             /* Number of output classes (V) */\n"
"        const int num_critical_pairs,      /* Number of critical pairs in the list */\n"
"        const float penalty_weight,        /* Amount to ADD to loss for critical error */\n"
"        const float reward_weight,         /* Amount to SUBTRACT from loss for high-confidence correct prediction */\n"
"        const float high_confidence_threshold /* Probability threshold for reward */\n"
"        )\n"
"{\n"
"    int idx = get_global_id(0); /* Global index for the sample/token */\n"
"\n"
"    if (idx < num_samples)\n"
"    {\n"
"        FP_TYPE current_loss = loss_in[idx];\n"
"        int target_label = targets[idx];\n"
"\n"
"        /* Handle padding/invalid target labels: Do not apply reward/penalty */\n"
"        if (target_label < 0 || target_label >= num_classes) {\n"
"            loss_out[idx] = current_loss;\n"
"            return;\n"
"        }\n"
"\n"
"        /* Find predicted class and its probability, and probability of correct class */\n"
"        size_t pred_offset = (size_t)idx * num_classes;\n"
"        int predicted_label = 0;\n"
"        FP_TYPE max_prob = -1.0f;\n"
"        for (int v = 0; v < num_classes; ++v) {\n"
"            FP_TYPE prob = predictions[pred_offset + v];\n"
"            if (prob > max_prob) {\n"
"                max_prob = prob;\n"
"                predicted_label = v;\n"
"            }\n"
"        }\n"
"        FP_TYPE correct_class_prob = predictions[pred_offset + target_label];\n"
"\n"
"        /* Calculate adjustment */\n"
"        float adjustment = 0.0f;\n"
"\n"
"        /* --- NEU: Penalty Logic mit Liste --- */\n"
"        bool is_critical_error = false;\n"
"        // Durchlaufe die Liste der kritischen Paare\n"
"        if (num_critical_pairs > 0 && critical_pairs != 0) { // Check for non-empty list and valid pointer\n"
"            for (int i = 0; i < num_critical_pairs; ++i) {\n"
"                int crit_target = critical_pairs[i * 2 + 0]; // Target ist an geraden Indizes\n"
"                int crit_pred   = critical_pairs[i * 2 + 1]; // Predicted ist an ungeraden Indizes\n"
"                if ((target_label == crit_target) && (predicted_label == crit_pred)) {\n"
"                    is_critical_error = true;\n"
"                    break; // Ein Treffer reicht\n"
"                }\n"
"            }\n"
"        }\n"
"        if (is_critical_error) {\n"
"            adjustment += penalty_weight;\n"
"        }\n"
"        /* --- ENDE NEU --- */\n"
"\n"
"        /* Reward Logic (unverändert) */\n"
"        bool is_correct = (predicted_label == target_label);\n"
"        bool is_high_confidence = (correct_class_prob >= high_confidence_threshold);\n"
"        if (is_correct && is_high_confidence) {\n"
"            adjustment -= reward_weight;\n"
"        }\n"
"\n"
"        /* Apply adjustment to the original loss */\n"
"        loss_out[idx] = current_loss + (FP_TYPE)adjustment;\n"
"    }\n"
"}";

const char *mycel_kernel_src =
"__kernel void mycel_reinforce(__global float* pheromone,\n"
"                              __global const int* neigh_idx,\n"
"                              __global const uchar* alive,\n"
"                              __global const float* mood,\n"
"                              __global const float* reinforce_gain,\n"
"                              __global const float* activity,\n"
"                              const int T_act,\n"
"                              const int T_cap,\n"
"                              const int K,\n"
"                              const int C) {\n"
"    if (get_global_id(0) != 0) {\n"
"        return;\n"
"    }\n"
"    for (int t = 0; t < T_act; ++t) {\n"
"        if (!alive[t]) {\n"
"            continue;\n"
"        }\n"
"        float act = activity ? activity[t] : 0.0f;\n"
"        if (act <= 0.0f) {\n"
"            continue;\n"
"        }\n"
"        for (int k = 0; k < K; ++k) {\n"
"            int nb = neigh_idx[t * K + k];\n"
"            if (nb < 0 || nb >= T_cap) {\n"
"                continue;\n"
"            }\n"
"            ulong edge = ((ulong)t * (ulong)K) + (ulong)k;\n"
"            for (int c = 0; c < C; ++c) {\n"
"                float mood_factor = mood[t * C + c];\n"
"                if (mood_factor == 0.0f) {\n"
"                    mood_factor = 1.0f;\n"
"                }\n"
"                ulong idx = edge * (ulong)C + (ulong)c;\n"
"                float delta = reinforce_gain[c] * act * mood_factor;\n"
"                float value = pheromone[idx] + delta;\n"
"                if (value < 0.0f) {\n"
"                    value = 0.0f;\n"
"                }\n"
"                pheromone[idx] = value;\n"
"            }\n"
"        }\n"
"    }\n"
"}\n"
"\n"
"__kernel void mycel_diffuse_decay(__global float* pheromone,\n"
"                                  __global const int* neigh_idx,\n"
"                                  __global const uchar* alive,\n"
"                                  __global const float* decay,\n"
"                                  __global const float* diffu,\n"
"                                  const int T_act,\n"
"                                  const int T_cap,\n"
"                                  const int K,\n"
"                                  const int C) {\n"
"    if (get_global_id(0) != 0) {\n"
"        return;\n"
"    }\n"
"    ulong edge_count = (ulong)T_cap * (ulong)K;\n"
"    for (ulong edge = 0; edge < edge_count; ++edge) {\n"
"        int t = (int)(edge / (ulong)K);\n"
"        if (t >= T_act || !alive[t]) {\n"
"            continue;\n"
"        }\n"
"        int nb = neigh_idx[edge];\n"
"        if (nb < 0 || nb >= T_cap || !alive[nb]) {\n"
"            continue;\n"
"        }\n"
"        float edge_decay = decay[edge];\n"
"        float edge_diffu = diffu[edge];\n"
"        for (int c = 0; c < C; ++c) {\n"
"            ulong idx = edge * (ulong)C + (ulong)c;\n"
"            float p = pheromone[idx];\n"
"            float neighbor_sum = 0.0f;\n"
"            int neighbor_deg = 0;\n"
"            for (int kk = 0; kk < K; ++kk) {\n"
"                int nb2 = neigh_idx[(ulong)nb * (ulong)K + (ulong)kk];\n"
"                if (nb2 < 0 || nb2 >= T_cap) {\n"
"                    continue;\n"
"                }\n"
"                ulong nidx = ((ulong)nb * (ulong)K + (ulong)kk) * (ulong)C + (ulong)c;\n"
"                neighbor_sum += pheromone[nidx];\n"
"                neighbor_deg += 1;\n"
"            }\n"
"            float neighbor_avg = (neighbor_deg > 0) ? (neighbor_sum / (float)neighbor_deg) : p;\n"
"            float value = p * (1.0f - edge_decay) + edge_diffu * (neighbor_avg - p);\n"
"            if (value < 0.0f) {\n"
"                value = 0.0f;\n"
"            }\n"
"            pheromone[idx] = value;\n"
"        }\n"
"    }\n"
"}\n"
"\n"
"__kernel void mycel_nutrient_update(__global float* nutrient,\n"
"                                    __global const uchar* alive,\n"
"                                    __global const float* activity,\n"
"                                    const float recovery,\n"
"                                    const int T_act) {\n"
"    if (get_global_id(0) != 0) {\n"
"        return;\n"
"    }\n"
"    for (int t = 0; t < T_act; ++t) {\n"
"        if (!alive[t]) {\n"
"            continue;\n"
"        }\n"
"        float act = activity ? activity[t] : 0.0f;\n"
"        float nu = nutrient[t] + act - recovery * nutrient[t];\n"
"        if (nu < 0.0f) {\n"
"            nu = 0.0f;\n"
"        }\n"
"        nutrient[t] = nu;\n"
"    }\n"
"}\n";

const char *subqg_simulation_kernel_src =
"#ifndef M_PI\n"
"#define M_PI 3.14159265358979323846\n"
"#endif\n"
"__kernel void subqg_simulation_step(\n"
"        __global FP_TYPE* energy,\n"
"        __global FP_TYPE* phase,\n"
"        __global FP_TYPE* interference_out,\n"
"        __global int* node_flag_out,\n"
"        __global int* spin_out,\n"
"        __global int* topology_out,\n"
"        __global const FP_TYPE* rng_energy,\n"
"        __global const FP_TYPE* rng_phase,\n"
"        __global const FP_TYPE* rng_spin,\n"
"        FP_TYPE noise_level,\n"
"        FP_TYPE threshold,\n"
"        FP_TYPE noise_factor,\n"
"        int cell_count,\n"
"        __global FP_TYPE* field_map,\n"
"        int write_field_map)\n"
"{\n"
"    int idx = get_global_id(0);\n"
"    if (idx >= cell_count) {\n"
"        return;\n"
"    }\n"
"    FP_TYPE rng_energy_val = rng_energy[idx];\n"
"    FP_TYPE rng_phase_val = rng_phase[idx];\n"
"    FP_TYPE rng_spin_val = rng_spin[idx];\n"
"    FP_TYPE current_energy = energy[idx];\n"
"    FP_TYPE current_phase = phase[idx];\n"
"    FP_TYPE effective_noise = noise_level * noise_factor;\n"
"    FP_TYPE energy_delta = (rng_energy_val - (FP_TYPE)0.5f) * effective_noise * (FP_TYPE)0.5f;\n"
"    FP_TYPE updated_energy = current_energy + energy_delta;\n"
"    if (updated_energy > (FP_TYPE)1.0f) updated_energy = (FP_TYPE)1.0f;\n"
"    if (updated_energy < (FP_TYPE)(-1.0f)) updated_energy = (FP_TYPE)(-1.0f);\n"
"    FP_TYPE clamped_phase = current_phase;\n"
"    if (clamped_phase > (FP_TYPE)1.0f) clamped_phase = (FP_TYPE)1.0f;\n"
"    if (clamped_phase < (FP_TYPE)(-1.0f)) clamped_phase = (FP_TYPE)(-1.0f);\n"
"    FP_TYPE phase_acc = asin(clamped_phase) / (FP_TYPE)M_PI;\n"
"    phase_acc += (rng_phase_val - (FP_TYPE)0.5f) * effective_noise * (FP_TYPE)0.2f;\n"
"    FP_TYPE updated_phase = sin(phase_acc * (FP_TYPE)M_PI);\n"
"    FP_TYPE interference = (updated_energy + updated_phase) * (FP_TYPE)0.5f;\n"
"    int node_flag = 0;\n"
"    int node_spin = 0;\n"
"    int topology = -1;\n"
"    FP_TYPE high_threshold = threshold + ((FP_TYPE)1.0f - threshold) * (FP_TYPE)0.66f;\n"
"    FP_TYPE mid_threshold = threshold + ((FP_TYPE)1.0f - threshold) * (FP_TYPE)0.33f;\n"
"    if (interference > threshold) {\n"
"        node_flag = 1;\n"
"        node_spin = (rng_spin_val > (FP_TYPE)0.5f) ? 1 : -1;\n"
"        if (interference > high_threshold) {\n"
"            topology = 2;\n"
"        } else if (interference > mid_threshold) {\n"
"            topology = 1;\n"
"        } else {\n"
"            topology = 0;\n"
"        }\n"
"    }\n"
"    energy[idx] = updated_energy;\n"
"    phase[idx] = updated_phase;\n"
"    interference_out[idx] = interference;\n"
"    node_flag_out[idx] = node_flag;\n"
"    spin_out[idx] = node_spin;\n"
"    topology_out[idx] = topology;\n"
"    if (write_field_map) {\n"
"        field_map[idx] = sin(updated_phase) * updated_energy;\n"
"    }\n"
"}\n";

const char *subqg_agent_kernel_src =
"typedef struct {\n"
"    float x;\n"
"    float y;\n"
"    float energy;\n"
"    float coupling;\n"
"} HPIOAgent;\n"
"__kernel void subqg_inject_agents(\n"
"        __global FP_TYPE* energy,\n"
"        __global FP_TYPE* phase,\n"
"        __global FP_TYPE* field_map,\n"
"        __global const HPIOAgent* agents,\n"
"        const int agent_count,\n"
"        const int grid_width,\n"
"        const int grid_height)\n"
"{\n"
"    int idx = get_global_id(0);\n"
"    int total = grid_width * grid_height;\n"
"    if (idx >= total) {\n"
"        return;\n"
"    }\n"
"    int x = idx % grid_width;\n"
"    int y = idx / grid_width;\n"
"    FP_TYPE local_energy = energy[idx];\n"
"    for (int i = 0; i < agent_count; ++i) {\n"
"        float dx = (float)x - agents[i].x;\n"
"        float dy = (float)y - agents[i].y;\n"
"        float dist = sqrt(dx * dx + dy * dy) + 1e-3f;\n"
"        float influence = agents[i].coupling / dist;\n"
"        local_energy += (FP_TYPE)(agents[i].energy * influence);\n"
"    }\n"
"    energy[idx] = local_energy;\n"
"    if (field_map) {\n"
"        field_map[idx] = sin(phase[idx]) * local_energy;\n"
"    }\n"
"}\n";
static const char * const sqse_kernel_src =
"#pragma OPENCL EXTENSION cl_khr_fp64 : enable\n"
"#define TWO_PI 6.283185307179586476925286766559f\n"
"inline float wrap_2pi(float x) {\n"
"    float y = fmod(x, TWO_PI);\n"
"    return (y < 0.0f) ? (y + TWO_PI) : y;\n"
"}\n"
"inline float mask_from_key(float key, float chaos_K) {\n"
"    float a = sin(key * 3.1415926535f + chaos_K * 0.5f);\n"
"    float b = cos(key * 2.7182818284f - chaos_K * 1.6180339887f);\n"
"    float c = a * b + sin((a - b) * 0.57721f + chaos_K);\n"
"    float m = fmod(fabs(c) * 123.4567f, TWO_PI);\n"
"    return m;\n"
"}\n"
"inline void stdmap_forward(float *theta, float *p, float K, int steps) {\n"
"    float th = *theta;\n"
"    float pp = *p;\n"
"    for (int t = 0; t < steps; ++t) {\n"
"        pp = wrap_2pi(pp + K * sin(th));\n"
"        th = wrap_2pi(th + pp);\n"
"    }\n"
"    *theta = th;\n"
"    *p = pp;\n"
"}\n"
"inline void stdmap_inverse(float *theta, float *p, float K, int steps) {\n"
"    float th = *theta;\n"
"    float pp = *p;\n"
"    for (int t = 0; t < steps; ++t) {\n"
"        float th_prev = wrap_2pi(th - pp);\n"
"        float pp_prev = wrap_2pi(pp - K * sin(th_prev));\n"
"        th = th_prev;\n"
"        pp = pp_prev;\n"
"    }\n"
"    *theta = th;\n"
"    *p = pp;\n"
"}\n"
"__kernel void sqse_encrypt(__global const float* data_in,\n"
"                           __global const float* key,\n"
"                           const float K,\n"
"                           const int steps,\n"
"                           __global float* out_theta,\n"
"                           __global float* out_p_masked,\n"
"                           const int n)\n"
"{\n"
"    int i = get_global_id(0);\n"
"    if (i >= n) return;\n"
"    float x = data_in[i];\n"
"    float k = key[i];\n"
"    float theta = fmod(fabs(x), 1.0f) * TWO_PI;\n"
"    float p     = fmod(fabs(k), 1.0f) * TWO_PI;\n"
"    stdmap_forward(&theta, &p, K, steps);\n"
"    float mask = mask_from_key(k, K);\n"
"    float p_masked = wrap_2pi(p + mask);\n"
"    out_theta[i]    = theta / TWO_PI;\n"
"    out_p_masked[i] = p_masked / TWO_PI;\n"
"}\n"
"__kernel void sqse_decrypt(__global const float* in_theta,\n"
"                           __global const float* in_p_masked,\n"
"                           __global const float* key,\n"
"                           const float K,\n"
"                           const int steps,\n"
"                           __global float* data_out,\n"
"                           const int n)\n"
"{\n"
"    int i = get_global_id(0);\n"
"    if (i >= n) return;\n"
"    float k = key[i];\n"
"    float theta = fmod(fabs(in_theta[i]), 1.0f) * TWO_PI;\n"
"    float p_m   = fmod(fabs(in_p_masked[i]), 1.0f) * TWO_PI;\n"
"    float mask = mask_from_key(k, K);\n"
"    float p = wrap_2pi(p_m - mask);\n"
"    stdmap_inverse(&theta, &p, K, steps);\n"
"    data_out[i] = theta / TWO_PI;\n"
"}\n";
// NOTE: Some OpenCL compilers (notably AMD's Windows stack) reserve the name
// "half" as a keyword for half-precision floats.  The shader source therefore
// avoids using "half" as a variable identifier to keep compilation portable.
const char *quantum_simulation_kernels_src =
"inline float2 complex_add(float2 a, float2 b) { return (float2)(a.x + b.x, a.y + b.y); }\n"
"inline float2 complex_sub(float2 a, float2 b) { return (float2)(a.x - b.x, a.y - b.y); }\n"
"inline float2 complex_mul(float2 a, float2 b) { return (float2)(a.x * b.x - a.y * b.y, a.x * b.y + a.y * b.x); }\n"
"inline float2 complex_scale(float2 a, float scale) { return (float2)(a.x * scale, a.y * scale); }\n"
"inline float complex_abs2(float2 a) { return a.x * a.x + a.y * a.y; }\n"
"\n"
"__kernel void quantum_apply_single_qubit(__global float2* state, const int target_qubit, const int num_qubits,\n"
"                                         float2 g00, float2 g01, float2 g10, float2 g11) {\n"
"    size_t pair_index = get_global_id(0);\n"
"    size_t total_pairs = ((size_t)1 << num_qubits) >> 1;\n"
"    if (pair_index >= total_pairs) return;\n"
"    size_t stride = (size_t)1 << target_qubit;\n"
"    size_t block = pair_index / stride;\n"
"    size_t offset = pair_index % stride;\n"
"    size_t base_index = block * (stride << 1) + offset;\n"
"    size_t index0 = base_index;\n"
"    size_t index1 = base_index + stride;\n"
"    float2 a0 = state[index0];\n"
"    float2 a1 = state[index1];\n"
"    float2 out0 = complex_add(complex_mul(g00, a0), complex_mul(g01, a1));\n"
"    float2 out1 = complex_add(complex_mul(g10, a0), complex_mul(g11, a1));\n"
"    state[index0] = out0;\n"
"    state[index1] = out1;\n"
"}\n"
"\n"
"__kernel void quantum_apply_controlled_phase(__global float2* state, const int control_qubit, const int target_qubit,\n"
"                                             const int num_qubits, float2 phase_factor) {\n"
"    size_t idx = get_global_id(0);\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    if (idx >= dimension) return;\n"
"    if ((((idx >> control_qubit) & 1) == 1) && (((idx >> target_qubit) & 1) == 1)) {\n"
"        state[idx] = complex_mul(state[idx], phase_factor);\n"
"    }\n"
"}\n"
"\n"
"__kernel void quantum_apply_controlled_not(__global float2* state, const int control_qubit, const int target_qubit, const int num_qubits) {\n"
"    size_t pair_index = get_global_id(0);\n"
"    size_t total_pairs = ((size_t)1 << num_qubits) >> 1;\n"
"    if (pair_index >= total_pairs) return;\n"
"    size_t stride = (size_t)1 << target_qubit;\n"
"    size_t block = pair_index / stride;\n"
"    size_t offset = pair_index % stride;\n"
"    size_t base_index = block * (stride << 1) + offset;\n"
"    size_t index0 = base_index;\n"
"    size_t index1 = base_index + stride;\n"
"    if (((index0 >> control_qubit) & 1) == 1) {\n"
"        float2 tmp = state[index0];\n"
"        state[index0] = state[index1];\n"
"        state[index1] = tmp;\n"
"    }\n"
"}\n"
"\n"
"__kernel void quantum_phase_oracle(__global float2* state, ulong mask, ulong value, const int num_qubits) {\n"
"    size_t idx = get_global_id(0);\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    if (idx >= dimension) return;\n"
"    if ( (idx & mask) == value ) {\n"
"        state[idx] = complex_scale(state[idx], -1.0f);\n"
"    }\n"
"}\n"
"\n"
"__kernel void quantum_phase_flip_except_zero(__global float2* state, const uint dimension) {\n"
"    size_t idx = get_global_id(0);\n"
"    if (idx >= dimension) return;\n"
"    if (idx != 0) {\n"
"        state[idx] = complex_scale(state[idx], -1.0f);\n"
"    }\n"
"}\n"
"\n"
"__kernel void quantum_modular_exponentiation(__global const float2* input_state, __global float2* output_state,\n"
"                                             const int num_control_qubits, const int num_work_qubits,\n"
"                                             const int base_a, const int modulus_N) {\n"
"    size_t idx = get_global_id(0);\n"
"    size_t total_qubits = (size_t)(num_control_qubits + num_work_qubits);\n"
"    size_t dimension = (size_t)1 << total_qubits;\n"
"    if (idx >= dimension) return;\n"
"    size_t work_mask = ((size_t)1 << num_work_qubits) - (size_t)1;\n"
"    size_t work_state = idx & work_mask;\n"
"    size_t control_state = idx >> num_work_qubits;\n"
"    size_t new_work_state = work_state;\n"
"    if (modulus_N > 1 && work_state < (size_t)modulus_N) {\n"
"        ulong exponent = control_state;\n"
"        ulong result = 1 % (ulong)modulus_N;\n"
"        ulong base_val = (ulong)base_a % (ulong)modulus_N;\n"
"        while (exponent > 0) {\n"
"            if (exponent & 1UL) {\n"
"                result = (result * base_val) % (ulong)modulus_N;\n"
"            }\n"
"            base_val = (base_val * base_val) % (ulong)modulus_N;\n"
"            exponent >>= 1;\n"
"        }\n"
"        new_work_state = (size_t)((result * (ulong)work_state) % (ulong)modulus_N);\n"
"    }\n"
"    size_t new_index = (control_state << num_work_qubits) | new_work_state;\n"
"    output_state[new_index] = input_state[idx];\n"
"}\n"
"\n"
"__kernel void quantum_swap_qubits(__global const float2* input_state, __global float2* output_state,\n"
"                                  const int qubit_a, const int qubit_b, const int num_qubits) {\n"
"    size_t idx = get_global_id(0);\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    if (idx >= dimension) return;\n"
"    size_t bit_a = (idx >> qubit_a) & 1UL;\n"
"    size_t bit_b = (idx >> qubit_b) & 1UL;\n"
"    size_t new_index = idx;\n"
"    if (bit_a != bit_b) {\n"
"        size_t mask = ((size_t)1 << qubit_a) | ((size_t)1 << qubit_b);\n"
"        new_index = idx ^ mask;\n"
"    }\n"
"    output_state[new_index] = input_state[idx];\n"
"}\n"
"\n"
"__kernel void quantum_compute_probabilities(__global const float2* state, __global float* probabilities, const int num_qubits) {\n"
"    size_t idx = get_global_id(0);\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    if (idx >= dimension) return;\n"
"    probabilities[idx] = complex_abs2(state[idx]);\n"
"}\n"
"\n"
"__kernel void quantum_expectation_pauli_z(__global const float2* state, __global float* expectation_terms,\n"
"                                          const int num_qubits, ulong z_mask) {\n"
"    size_t idx = get_global_id(0);\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    if (idx >= dimension) return;\n"
"    ulong masked = ((ulong)idx) & z_mask;\n"
"    uint parity = (uint)popcount(masked);\n"
"    float sign = (parity & 1U) ? -1.0f : 1.0f;\n"
"    expectation_terms[idx] = sign * complex_abs2(state[idx]);\n"
"}\n"
"\n"
"inline void initialize_zero_state_vec(__global float2* state, size_t dimension) {\n"
"    for (size_t i = 0; i < dimension; ++i) {\n"
"        state[i] = (float2)(0.0f, 0.0f);\n"
"    }\n"
"    if (dimension > 0) {\n"
"        state[0] = (float2)(1.0f, 0.0f);\n"
"    }\n"
"}\n"
"\n"
"inline void apply_single_qubit_gate_vec(__global float2* state, int num_qubits, int target,\n"
"                                        float2 g00, float2 g01, float2 g10, float2 g11) {\n"
"    if (target < 0 || target >= num_qubits) {\n"
"        return;\n"
"    }\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    size_t stride = (size_t)1 << target;\n"
"    size_t step = stride << 1;\n"
"    for (size_t base = 0; base < dimension; base += step) {\n"
"        for (size_t offset = 0; offset < stride; ++offset) {\n"
"            size_t index0 = base + offset;\n"
"            size_t index1 = index0 + stride;\n"
"            float2 a0 = state[index0];\n"
"            float2 a1 = state[index1];\n"
"            state[index0] = complex_add(complex_mul(g00, a0), complex_mul(g01, a1));\n"
"            state[index1] = complex_add(complex_mul(g10, a0), complex_mul(g11, a1));\n"
"        }\n"
"    }\n"
"}\n"
"\n"
"inline void apply_rotation_y_vec(__global float2* state, int num_qubits, int target, float theta) {\n"
"    float theta_half = 0.5f * theta;\n"
"    float c = cos(theta_half);\n"
"    float s = sin(theta_half);\n"
"    float2 g00 = (float2)(c, 0.0f);\n"
"    float2 g01 = (float2)(-s, 0.0f);\n"
"    float2 g10 = (float2)(s, 0.0f);\n"
"    float2 g11 = (float2)(c, 0.0f);\n"
"    apply_single_qubit_gate_vec(state, num_qubits, target, g00, g01, g10, g11);\n"
"}\n"
"\n"
"inline void apply_rotation_z_vec(__global float2* state, int num_qubits, int target, float theta) {\n"
"    float theta_half = 0.5f * theta;\n"
"    float c = cos(theta_half);\n"
"    float s = sin(theta_half);\n"
"    float2 g00 = (float2)(c, -s);\n"
"    float2 g01 = (float2)(0.0f, 0.0f);\n"
"    float2 g10 = (float2)(0.0f, 0.0f);\n"
"    float2 g11 = (float2)(c, s);\n"
"    apply_single_qubit_gate_vec(state, num_qubits, target, g00, g01, g10, g11);\n"
"}\n"
"\n"
"inline void apply_cnot_vec(__global float2* state, int num_qubits, int control, int target) {\n"
"    if (control < 0 || control >= num_qubits || target < 0 || target >= num_qubits) {\n"
"        return;\n"
"    }\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    size_t control_mask = (size_t)1 << control;\n"
"    size_t target_mask = (size_t)1 << target;\n"
"    for (size_t idx = 0; idx < dimension; ++idx) {\n"
"        if ((idx & control_mask) && ((idx & target_mask) == 0)) {\n"
"            size_t swap_idx = idx | target_mask;\n"
"            float2 tmp = state[idx];\n"
"            state[idx] = state[swap_idx];\n"
"            state[swap_idx] = tmp;\n"
"        }\n"
"    }\n"
"}\n"
"\n"
"inline void apply_vqe_ansatz_with_shift(__global float2* state, int num_qubits, int ansatz_layers,\n"
"                                       __global const float* parameters, int num_parameters,\n"
"                                       int shift_index, float shift_amount) {\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    initialize_zero_state_vec(state, dimension);\n"
"    if (num_qubits <= 0 || ansatz_layers <= 0) {\n"
"        return;\n"
"    }\n"
"    int params_per_layer = 2 * num_qubits;\n"
"    for (int layer = 0; layer < ansatz_layers; ++layer) {\n"
"        int base = layer * params_per_layer;\n"
"        for (int q = 0; q < num_qubits; ++q) {\n"
"            int param_idx = base + q;\n"
"            float theta = (param_idx < num_parameters) ? parameters[param_idx] : 0.0f;\n"
"            if (param_idx == shift_index) {\n"
"                theta += shift_amount;\n"
"            }\n"
"            apply_rotation_y_vec(state, num_qubits, q, theta);\n"
"        }\n"
"        for (int q = 0; q < num_qubits; ++q) {\n"
"            int param_idx = base + num_qubits + q;\n"
"            float theta = (param_idx < num_parameters) ? parameters[param_idx] : 0.0f;\n"
"            if (param_idx == shift_index) {\n"
"                theta += shift_amount;\n"
"            }\n"
"            apply_rotation_z_vec(state, num_qubits, q, theta);\n"
"        }\n"
"        for (int q = 0; q < num_qubits - 1; ++q) {\n"
"            apply_cnot_vec(state, num_qubits, q, q + 1);\n"
"        }\n"
"        if (num_qubits > 1) {\n"
"            apply_cnot_vec(state, num_qubits, num_qubits - 1, 0);\n"
"        }\n"
"    }\n"
"}\n"
"\n"
"inline float compute_pauli_z_energy_vec(__global const float2* state, int num_qubits,\n"
"                                       __global const ulong* term_masks,\n"
"                                       __global const float* term_coeffs, int num_terms) {\n"
"    if (!term_masks || !term_coeffs || num_terms <= 0) {\n"
"        return 0.0f;\n"
"    }\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    float energy = 0.0f;\n"
"    for (int term = 0; term < num_terms; ++term) {\n"
"        ulong mask = term_masks[term];\n"
"        float coeff = term_coeffs[term];\n"
"        float expectation = 0.0f;\n"
"        for (size_t idx = 0; idx < dimension; ++idx) {\n"
"            float2 amp = state[idx];\n"
"            float prob = amp.x * amp.x + amp.y * amp.y;\n"
"            ulong masked = ((ulong)idx) & mask;\n"
"            uint parity = (uint)popcount(masked);\n"
"            float sign = (parity & 1U) ? -1.0f : 1.0f;\n"
"            expectation += sign * prob;\n"
"        }\n"
"        energy += coeff * expectation;\n"
"    }\n"
"    return energy;\n"
"}\n"
"\n"
"__kernel void vqe_gradient_batch_kernel(__global float* gradients_out,\n"
"                                       __global const float* parameters,\n"
"                                       const int num_parameters, const int num_qubits,\n"
"                                       const int ansatz_layers,\n"
"                                       __global const ulong* term_masks,\n"
"                                       __global const float* term_coeffs, const int num_terms,\n"
"                                       __global float2* state_workspace) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)num_parameters) {\n"
"        return;\n"
"    }\n"
"    size_t dimension = (size_t)1 << num_qubits;\n"
"    __global float2* state = state_workspace + gid * dimension;\n"
"    const float shift = 1.5707963267948966f;\n"
"    apply_vqe_ansatz_with_shift(state, num_qubits, ansatz_layers, parameters, num_parameters, (int)gid, shift);\n"
"    float energy_plus = compute_pauli_z_energy_vec(state, num_qubits, term_masks, term_coeffs, num_terms);\n"
"    apply_vqe_ansatz_with_shift(state, num_qubits, ansatz_layers, parameters, num_parameters, (int)gid, -shift);\n"
"    float energy_minus = compute_pauli_z_energy_vec(state, num_qubits, term_masks, term_coeffs, num_terms);\n"
"    gradients_out[gid] = 0.5f * (energy_plus - energy_minus);\n"
"}\n"
"\n"
"inline void atomic_add_float_global(volatile __global float* addr, float value) {\n"
"    union { unsigned int u; float f; } old_val;\n"
"    union { unsigned int u; float f; } new_val;\n"
"    do {\n"
"        old_val.f = *addr;\n"
"        new_val.f = old_val.f + value;\n"
"    } while (atomic_cmpxchg((volatile __global int*)addr, as_int(old_val.f), as_int(new_val.f)) != as_int(old_val.f));\n"
"}\n"
"\n"
"__kernel void qualia_resonator_kernel(__global const float* gradient_signal,\n"
"                                      __global const float* field_flux_signal,\n"
"                                      __global const float* coherence_signal,\n"
"                                      __global const float* novelty_signal,\n"
"                                      __global float* resonance_field,\n"
"                                      __global float* resonance_vector,\n"
"                                      const int signal_count,\n"
"                                      const float mood_bias,\n"
"                                      const float harmony_gain) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    float grad = gradient_signal[gid];\n"
"    float flux = field_flux_signal[gid];\n"
"    float coherence = coherence_signal[gid];\n"
"    float novelty = novelty_signal[gid];\n"
"    float clarity = coherence / (1.0f + fabs(grad));\n"
"    float tension = fabs(grad - flux);\n"
"    float wellbeing = fmax(0.0f, harmony_gain - fabs(flux - mood_bias));\n"
"    float curiosity = fabs(novelty - mood_bias);\n"
"    float resonance = 0.25f * clarity + 0.35f * wellbeing - 0.2f * tension + 0.2f * curiosity;\n"
"    resonance_field[gid] = resonance;\n"
"    atomic_add_float_global(resonance_vector + 0, clarity);\n"
"    atomic_add_float_global(resonance_vector + 1, tension);\n"
"    atomic_add_float_global(resonance_vector + 2, wellbeing);\n"
"    atomic_add_float_global(resonance_vector + 3, curiosity);\n"
"}\n"
"\n"
"__kernel void intuition_precognition_kernel(__global const float* pheromone_signal,\n"
"                                            __global const float* field_signal,\n"
"                                            __global const float* quantum_signal,\n"
"                                            __global float* foresight_field,\n"
"                                            __global float* intuition_vector,\n"
"                                            const int signal_count,\n"
"                                            const float sensitivity,\n"
"                                            const float anticipation_gain) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    float pher = pheromone_signal[gid];\n"
"    float field = field_signal[gid];\n"
"    float quantum = quantum_signal[gid];\n"
"    float alignment = 0.5f * (pher + field);\n"
"    float interference = fabs(field - quantum);\n"
"    float anticipation = alignment - sensitivity * interference;\n"
"    float confidence = fmax(0.0f, 1.0f - sensitivity * fabs(quantum));\n"
"    float foresight = anticipation_gain * anticipation * confidence;\n"
"    foresight_field[gid] = foresight;\n"
"    atomic_add_float_global(intuition_vector + 0, anticipation);\n"
"    atomic_add_float_global(intuition_vector + 1, confidence);\n"
"    atomic_add_float_global(intuition_vector + 2, interference);\n"
"}\n"
"\n"
"__kernel void context_resonance_kernel(__global const float* stimulus_signal,\n"
"                                      __global const float* response_signal,\n"
"                                      __global const float* valence_signal,\n"
"                                      __global float* context_field,\n"
"                                      __global float* context_vector,\n"
"                                      const int signal_count,\n"
"                                      const float recency_bias,\n"
"                                      const float significance_scale) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    float stimulus = stimulus_signal[gid];\n"
"    float response = response_signal[gid];\n"
"    float valence = valence_signal[gid];\n"
"    float concordance = stimulus * response;\n"
"    float resonance = concordance * recency_bias + valence * significance_scale;\n"
"    float dissonance = fabs(stimulus - response);\n"
"    context_field[gid] = resonance;\n"
"    atomic_add_float_global(context_vector + 0, resonance);\n"
"    atomic_add_float_global(context_vector + 1, dissonance);\n"
"    atomic_add_float_global(context_vector + 2, valence);\n"
"}\n"
"\n"
"__kernel void dream_state_generator_kernel(\n"
"    __global const float* qualia_vector,\n"
"    __global const float* intuition_vector,\n"
"    __global const float* context_vector,\n"
"    __global const float* gradient_signal,\n"
"    __global const float* flux_signal,\n"
"    __global const float* field_signal,\n"
"    __global const float* behavior_signal,\n"
"    __global const float* target_qualia,\n"
"    __global float* ideal_gradient_out,\n"
"    __global float* ideal_flux_out,\n"
"    __global float* ideal_field_out,\n"
"    __global float* ideal_behavior_out,\n"
"    __global float* latent_out,\n"
"    const int signal_count) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    float clarity_now = qualia_vector[0];\n"
"    float tension_now = qualia_vector[1];\n"
"    float wellbeing_now = qualia_vector[2];\n"
"    float curiosity_now = qualia_vector[3];\n"
"    float anticipation = intuition_vector[0];\n"
"    float confidence = intuition_vector[1];\n"
"    float interference = intuition_vector[2];\n"
"    float resonance = context_vector[0];\n"
"    float dissonance = context_vector[1];\n"
"    float valence = context_vector[2];\n"
"    float clarity_target = target_qualia[0];\n"
"    float tension_target = target_qualia[1];\n"
"    float wellbeing_target = target_qualia[2];\n"
"    float curiosity_target = target_qualia[3];\n"
"    float clarity_delta = clarity_target - clarity_now;\n"
"    float tension_delta = tension_target - tension_now;\n"
"    float wellbeing_delta = wellbeing_target - wellbeing_now;\n"
"    float curiosity_delta = curiosity_target - curiosity_now;\n"
"    float blend_seed = 0.5f * (confidence + valence) - 0.25f * (interference + dissonance);\n"
"    float safety = fmax(0.05f, 0.25f + 0.25f * (anticipation + resonance));\n"
"    float blend = clamp(blend_seed * safety + 0.5f, 0.0f, 1.0f);\n"
"    float grad = gradient_signal[gid];\n"
"    float flux = flux_signal[gid];\n"
"    float field = field_signal[gid];\n"
"    float behavior = behavior_signal[gid];\n"
"    float desired_grad = grad - tension_delta + 0.5f * clarity_delta;\n"
"    float desired_flux = flux + 0.5f * wellbeing_delta - 0.25f * tension_delta;\n"
"    float desired_field = field + 0.25f * clarity_delta + 0.25f * wellbeing_delta;\n"
"    float desired_behavior = behavior + 0.2f * curiosity_delta + 0.2f * valence;\n"
"    ideal_gradient_out[gid] = mad(blend, desired_grad - grad, grad);\n"
"    ideal_flux_out[gid] = mad(blend, desired_flux - flux, flux);\n"
"    ideal_field_out[gid] = mad(blend, desired_field - field, field);\n"
"    ideal_behavior_out[gid] = mad(blend, desired_behavior - behavior, behavior);\n"
"    if (latent_out) {\n"
"        size_t base = gid * 4;\n"
"        latent_out[base + 0] = clarity_delta;\n"
"        latent_out[base + 1] = tension_delta;\n"
"        latent_out[base + 2] = wellbeing_delta;\n"
"        latent_out[base + 3] = curiosity_delta;\n"
"    }\n"
"}\n"
"\n"
"__kernel void transformation_planner_kernel(\n"
"    __global const float* current_gradient,\n"
"    __global const float* current_flux,\n"
"    __global const float* dream_gradient,\n"
"    __global const float* dream_flux,\n"
"    __global const float* qualia_vector,\n"
"    __global const float* context_vector,\n"
"    __global float* plan_matrix,\n"
"    __global float* plan_scores,\n"
"    const int signal_count,\n"
"    const float learning_rate,\n"
"    const float exploration_bias) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    float grad_now = current_gradient[gid];\n"
"    float flux_now = current_flux[gid];\n"
"    float grad_target = dream_gradient[gid];\n"
"    float flux_target = dream_flux[gid];\n"
"    float clarity = qualia_vector[0];\n"
"    float tension = qualia_vector[1];\n"
"    float wellbeing = qualia_vector[2];\n"
"    float curiosity = qualia_vector[3];\n"
"    float resonance = context_vector[0];\n"
"    float dissonance = context_vector[1];\n"
"    float valence = context_vector[2];\n"
"    float grad_delta = grad_target - grad_now;\n"
"    float flux_delta = flux_target - flux_now;\n"
"    float magnitude = hypot(grad_delta, flux_delta);\n"
"    float caution = clamp(1.0f - tension, 0.1f, 1.0f);\n"
"    float drive = clamp(clarity + wellbeing + resonance, 0.0f, 4.0f);\n"
"    float exploration = exploration_bias + 0.25f * curiosity;\n"
"    float plan_score = magnitude * learning_rate * caution + valence - dissonance;\n"
"    plan_score = fmax(plan_score, 0.0f);\n"
"    plan_scores[gid] = plan_score;\n"
"    size_t base = gid * 4;\n"
"    float norm = magnitude > 0.0f ? (1.0f / magnitude) : 0.0f;\n"
"    float param_adjust = grad_delta * norm;\n"
"    float topology_adjust = flux_delta * norm;\n"
"    float anneal_push = drive * 0.25f;\n"
"    float prototype_hint = exploration;\n"
"    plan_matrix[base + 0] = param_adjust * learning_rate;\n"
"    plan_matrix[base + 1] = topology_adjust * learning_rate;\n"
"    plan_matrix[base + 2] = anneal_push;\n"
"    plan_matrix[base + 3] = prototype_hint;\n"
"}\n"
"\n"
"__kernel void generate_system_narrative_kernel(\n"
"    __global const float* qualia_vector,\n"
"    __global const float* intuition_vector,\n"
"    __global const float* context_vector,\n"
"    __global const float* dream_latent,\n"
"    __global const float* plan_matrix,\n"
"    __global const float* plan_scores,\n"
"    __global float* narrative_embeddings,\n"
"    __global float* narrative_weights,\n"
"    __global float* narrative_summary,\n"
"    const int signal_count,\n"
"    const int latent_stride,\n"
"    const int plan_stride) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    float clarity = qualia_vector[0];\n"
"    float tension = qualia_vector[1];\n"
"    float wellbeing = qualia_vector[2];\n"
"    float curiosity = qualia_vector[3];\n"
"    float anticipation = intuition_vector[0];\n"
"    float confidence = intuition_vector[1];\n"
"    float interference = intuition_vector[2];\n"
"    float resonance = context_vector[0];\n"
"    float dissonance = context_vector[1];\n"
"    float valence = context_vector[2];\n"
"    int latent_base = gid * latent_stride;\n"
"    int plan_base = gid * plan_stride;\n"
"    float latent_clarity = (latent_stride > 0) ? dream_latent[latent_base + 0] : 0.0f;\n"
"    float latent_tension = (latent_stride > 1) ? dream_latent[latent_base + 1] : 0.0f;\n"
"    float latent_wellbeing = (latent_stride > 2) ? dream_latent[latent_base + 2] : 0.0f;\n"
"    float latent_curiosity = (latent_stride > 3) ? dream_latent[latent_base + 3] : 0.0f;\n"
"    float directive_focus = (plan_stride > 0) ? plan_matrix[plan_base + 0] : 0.0f;\n"
"    float directive_explore = (plan_stride > 1) ? plan_matrix[plan_base + 1] : 0.0f;\n"
"    float directive_stability = (plan_stride > 2) ? plan_matrix[plan_base + 2] : 0.0f;\n"
"    float directive_delta = (plan_stride > 3) ? plan_matrix[plan_base + 3] : 0.0f;\n"
"    float score = plan_scores ? plan_scores[gid] : 0.0f;\n"
"    float narrative_drive = directive_focus + latent_clarity - latent_tension;\n"
"    float narrative_equilibrium = directive_stability + wellbeing - tension;\n"
"    float narrative_curiosity = directive_explore + curiosity + latent_curiosity;\n"
"    float narrative_affect = valence + confidence - interference - dissonance;\n"
"    size_t embed_base = gid * 4;\n"
"    narrative_embeddings[embed_base + 0] = narrative_drive;\n"
"    narrative_embeddings[embed_base + 1] = narrative_equilibrium;\n"
"    narrative_embeddings[embed_base + 2] = narrative_curiosity;\n"
"    narrative_embeddings[embed_base + 3] = directive_delta;\n"
"    float weight = clamp(0.5f * fabs(narrative_drive) + 0.35f * fabs(narrative_equilibrium) +\n"
"                        0.25f * fabs(narrative_affect) + 0.1f * score, 0.0f, 16.0f);\n"
"    narrative_weights[gid] = weight;\n"
"    if (narrative_summary) {\n"
"        atomic_add_float_global(narrative_summary + 0, narrative_drive);\n"
"        atomic_add_float_global(narrative_summary + 1, narrative_equilibrium);\n"
"        atomic_add_float_global(narrative_summary + 2, narrative_curiosity);\n"
"        atomic_add_float_global(narrative_summary + 3, narrative_affect);\n"
"    }\n"
"}\n"
"\n"
"__kernel void abstract_to_symbolic_concepts_kernel(\n"
"    __global const float* narrative_embeddings,\n"
"    __global const float* narrative_weights,\n"
"    __global float* concept_codes,\n"
"    __global float* concept_strength,\n"
"    __global float* concept_summary,\n"
"    const int signal_count,\n"
"    const int embedding_stride) {\n"
"    size_t gid = get_global_id(0);\n"
"    if (gid >= (size_t)signal_count) {\n"
"        return;\n"
"    }\n"
"    size_t base = gid * embedding_stride;\n"
"    float drive = (embedding_stride > 0) ? narrative_embeddings[base + 0] : 0.0f;\n"
"    float equilibrium = (embedding_stride > 1) ? narrative_embeddings[base + 1] : 0.0f;\n"
"    float curiosity = (embedding_stride > 2) ? narrative_embeddings[base + 2] : 0.0f;\n"
"    float delta = (embedding_stride > 3) ? narrative_embeddings[base + 3] : 0.0f;\n"
"    float weight = narrative_weights ? narrative_weights[gid] : 0.0f;\n"
"    float pattern_energy = fabs(drive) + 0.5f * fabs(equilibrium) + 0.25f * fabs(curiosity);\n"
"    float coherence = 1.0f / (1.0f + fabs(delta));\n"
"    float activation = weight * coherence + 0.5f * pattern_energy;\n"
"    float archetype_axis = atan2(drive, equilibrium);\n"
"    float symbol_id = (archetype_axis * 3.18309886184f) + curiosity;\n"
"    concept_codes[gid] = symbol_id;\n"
"    concept_strength[gid] = activation;\n"
"    if (concept_summary) {\n"
"        atomic_add_float_global(concept_summary + 0, symbol_id);\n"
"        atomic_add_float_global(concept_summary + 1, activation);\n"
"        atomic_add_float_global(concept_summary + 2, pattern_energy);\n"
"        atomic_add_float_global(concept_summary + 3, coherence);\n"
"    }\n"
"}\n";

// ----------------------------------------------------------------------------------

// --- Helper Function Implementations ---

/**
 * @brief Retrieve profiling counters from the most recent quantum echo execution.
 *
 * @param out_profile Destination pointer receiving the collected counters.
 *
 * @return 1 on success, 0 if @p out_profile is NULL.
 */
DLLEXPORT int get_last_quantum_echo_profile(QuantumEchoProfile* out_profile) {
    if (!out_profile) {
        return 0;
    }
    *out_profile = g_last_quantum_echo_profile;
    return 1;
}

/**
 * @brief Compiles an OpenCL kernel from source code.
 */
cl_int compile_opencl_kernel_variant(const char* kernel_source, const char* kernel_name,
                                     cl_program* program_out, cl_kernel* kernel_out,
                                     int enable_fast_math) {
    cl_int err;
    size_t source_size;

    // Initialize output pointers
    *program_out = NULL;
    *kernel_out = NULL;

    if (!kernel_source) {
         fprintf(stderr, "[C] compile_opencl_kernel: Error - kernel_source is NULL for '%s'.\n", kernel_name ? kernel_name : "UNKNOWN");
         return CL_INVALID_VALUE;
    }
    source_size = strlen(kernel_source);

    if (!context || !device_id) {
        fprintf(stderr, "[C] compile_opencl_kernel: Error - No context or device available for compiling '%s'.\n", kernel_name ? kernel_name : "UNKNOWN");
        return CL_INVALID_CONTEXT; // Or CL_INVALID_DEVICE if more appropriate
    }

    // Create program object
    *program_out = clCreateProgramWithSource(context, 1, &kernel_source, &source_size, &err);
    if (!*program_out || err != CL_SUCCESS) {
        fprintf(stderr, "[C] compile_opencl_kernel: clCreateProgramWithSource failed for '%s': %s (%d)\n",
                kernel_name ? kernel_name : "UNKNOWN", clGetErrorString(err), err);
        return err;
    }

    // --- Construct Build Options ---
    const char* math_optimizations;
    if (enable_fast_math) {
        math_optimizations =
            "-cl-fast-relaxed-math "
            "-cl-mad-enable "
            "-cl-no-signed-zeros "
            "-cl-unsafe-math-optimizations "
            "-DFAST_MATH -DENABLE_FAST_VARIANT";
    } else {
        math_optimizations =
            "-cl-finite-math-only "
            "-cl-denorms-are-zero "
            "-DENABLE_FAST_VARIANT=0";
    }

    char build_options[512];
    snprintf(build_options, sizeof(build_options),
             "-cl-std=CL1.2 -Werror %s -D FP_TYPE=%s %s %s %s -DFP_TYPE_SIZE=%zu",
             math_optimizations,
             KERNEL_FP_TYPE_STR,
             has_fp64_support ? "-D CL_HAS_FP64" : "",          // Define if FP64 is supported
             has_atomics_support ? "-D CL_HAS_ATOMICS" : "",    // Define if required KHR atomics are supported
             has_int64_atomics ? "-D CL_HAS_INT64_ATOMICS" : "",// Flag availability of 64-bit atomics
             sizeof(FP_TYPE)                                    // Define FP_TYPE_SIZE
             );
    build_options[sizeof(build_options) - 1] = '\0'; // Ensure null termination

    // Build the program
    err = clBuildProgram(*program_out, 1, &device_id, build_options, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] compile_opencl_kernel: clBuildProgram failed for '%s' with options '%s': %s (%d)\n",
                kernel_name ? kernel_name : "UNKNOWN", build_options, clGetErrorString(err), err);

        // Get and print the build log
        size_t log_size = 0;
        clGetProgramBuildInfo(*program_out, device_id, CL_PROGRAM_BUILD_LOG, 0, NULL, &log_size);
        if (log_size > 1) {
            char *log = (char *)malloc(log_size);
            if (log) {
                clGetProgramBuildInfo(*program_out, device_id, CL_PROGRAM_BUILD_LOG, log_size, log, NULL);
                fprintf(stderr, "--- OpenCL Build Log (%s) ---\n%s\n-----------------------------\n", kernel_name ? kernel_name : "UNKNOWN", log);
                free(log);
            } else {
                fprintf(stderr, "[C] compile_opencl_kernel: Failed to allocate memory (%zu bytes) for build log.\n", log_size);
            }
        } else {
             fprintf(stderr, "[C] compile_opencl_kernel: Build log is empty or unavailable.\n");
        }

        // Cleanup partially created resources
        clReleaseProgram(*program_out);
        *program_out = NULL;
        return err;
    }

    // Create the kernel object
    *kernel_out = clCreateKernel(*program_out, kernel_name, &err);
    if (!*kernel_out || err != CL_SUCCESS) {
        fprintf(stderr, "[C] compile_opencl_kernel: clCreateKernel failed for '%s': %s (%d)\n",
                kernel_name ? kernel_name : "UNKNOWN", clGetErrorString(err), err);
        // Cleanup program if kernel creation fails
        clReleaseProgram(*program_out);
        *program_out = NULL;
        return err;
    }

    // Success
    return CL_SUCCESS;
}

cl_int compile_opencl_kernel_dual(const char* kernel_source, const char* kernel_name,
                                  cl_program* strict_program_out, cl_kernel* strict_kernel_out,
                                  cl_program* fast_program_out, cl_kernel* fast_kernel_out) {
    cl_int err = compile_opencl_kernel_variant(kernel_source, kernel_name,
                                               strict_program_out, strict_kernel_out, 0);
    if (err != CL_SUCCESS) {
        return err;
    }
    err = compile_opencl_kernel_variant(kernel_source, kernel_name,
                                        fast_program_out, fast_kernel_out, 1);
    if (err != CL_SUCCESS) {
        return err;
    }
    return CL_SUCCESS;
}

static int ensure_sqse_kernels_ready(void) {
    if (sqse_program && sqse_encrypt_kernel && sqse_decrypt_kernel) {
        return 1;
    }
    if (!context || !device_id) {
        fprintf(stderr, "[C] SQSE: OpenCL context/device not initialized. Call initialize_gpu first.\n");
        return 0;
    }

    cl_program program = NULL;
    cl_kernel encrypt = NULL;
    cl_int err = compile_opencl_kernel_variant(sqse_kernel_src, "sqse_encrypt", &program, &encrypt, 0);
    if (err != CL_SUCCESS || !program || !encrypt) {
        fprintf(stderr, "[C] SQSE: Failed to compile sqse_encrypt kernel: %s (%d)\n", clGetErrorString(err), err);
        if (program) { clReleaseProgram(program); }
        if (encrypt) { clReleaseKernel(encrypt); }
        return 0;
    }

    cl_int derr = CL_SUCCESS;
    cl_kernel decrypt = clCreateKernel(program, "sqse_decrypt", &derr);
    if (derr != CL_SUCCESS || !decrypt) {
        fprintf(stderr, "[C] SQSE: Failed to create sqse_decrypt kernel: %s (%d)\n", clGetErrorString(derr), derr);
        clReleaseKernel(encrypt);
        clReleaseProgram(program);
        return 0;
    }

    sqse_program = program;
    sqse_encrypt_kernel = encrypt;
    sqse_decrypt_kernel = decrypt;
    return 1;
}

/**
 * @brief Releases all allocated OpenCL resources.
 */
void shutdown_driver() {
    printf("[C] shutdown_driver: Starting OpenCL resource cleanup...\n");

    // Release all kernel objects
    #define RELEASE_KERNEL(k) if (k) { clReleaseKernel(k); k = NULL; }
    RELEASE_KERNEL(matmul_kernel);
    RELEASE_KERNEL(matmul_kernel_fast);
    RELEASE_KERNEL(softmax_kernel);
    RELEASE_KERNEL(softmax_kernel_fast);
    RELEASE_KERNEL(gelu_kernel);
    RELEASE_KERNEL(gelu_kernel_fast);
    RELEASE_KERNEL(add_kernel);
    RELEASE_KERNEL(add_kernel_fast);
    RELEASE_KERNEL(mul_kernel);
    RELEASE_KERNEL(mul_kernel_fast);
    RELEASE_KERNEL(layernorm_kernel);
    RELEASE_KERNEL(layernorm_kernel_fast);
    RELEASE_KERNEL(transpose_kernel);
    RELEASE_KERNEL(transpose_kernel_fast);
    RELEASE_KERNEL(gelu_backward_kernel);
    RELEASE_KERNEL(gelu_backward_kernel_fast);
    RELEASE_KERNEL(matmul_backward_da_kernel);
    RELEASE_KERNEL(matmul_backward_da_kernel_fast);
    RELEASE_KERNEL(matmul_backward_db_kernel);
    RELEASE_KERNEL(matmul_backward_db_kernel_fast);
    RELEASE_KERNEL(layernorm_backward_kernel);
    RELEASE_KERNEL(layernorm_backward_kernel_fast);
    RELEASE_KERNEL(adam_kernel);
    RELEASE_KERNEL(adam_kernel_fast);
    RELEASE_KERNEL(softmax_backward_kernel);
    RELEASE_KERNEL(softmax_backward_kernel_fast);
    RELEASE_KERNEL(mul_backward_kernel);
    RELEASE_KERNEL(mul_backward_kernel_fast);
    RELEASE_KERNEL(transpose_backward_kernel);
    RELEASE_KERNEL(transpose_backward_kernel_fast);
    RELEASE_KERNEL(embedding_lookup_kernel);
    RELEASE_KERNEL(embedding_lookup_kernel_fast);
    RELEASE_KERNEL(reduce_sum_kernel);
    RELEASE_KERNEL(reduce_sum_kernel_fast);
    RELEASE_KERNEL(broadcast_add_kernel);
    RELEASE_KERNEL(broadcast_add_kernel_fast);
    RELEASE_KERNEL(transpose_batched_kernel);
    RELEASE_KERNEL(transpose_batched_kernel_fast);
    RELEASE_KERNEL(transpose_12_batched_kernel);
    RELEASE_KERNEL(transpose_12_batched_kernel_fast);
    RELEASE_KERNEL(matmul_batched_kernel);
    RELEASE_KERNEL(matmul_batched_kernel_fast);
    RELEASE_KERNEL(matmul_batched_backward_da_kernel);
    RELEASE_KERNEL(matmul_batched_backward_da_kernel_fast);
    RELEASE_KERNEL(matmul_batched_backward_db_kernel);
    RELEASE_KERNEL(matmul_batched_backward_db_kernel_fast);
    RELEASE_KERNEL(log_softmax_kernel);
    RELEASE_KERNEL(log_softmax_kernel_fast);
    RELEASE_KERNEL(cross_entropy_kernel);
    RELEASE_KERNEL(cross_entropy_kernel_fast);
    RELEASE_KERNEL(add_broadcast_pe_kernel);
    RELEASE_KERNEL(add_broadcast_pe_kernel_fast);
    RELEASE_KERNEL(threshold_spike_kernel);
    RELEASE_KERNEL(threshold_spike_kernel_fast);
    RELEASE_KERNEL(add_bias_mn_kernel);
    RELEASE_KERNEL(add_bias_mn_kernel_fast);
    RELEASE_KERNEL(dynamic_token_assign_kernel);
    RELEASE_KERNEL(dynamic_token_assign_kernel_fast);
    RELEASE_KERNEL(pairwise_similarity_kernel);
    RELEASE_KERNEL(pairwise_similarity_kernel_fast);
    RELEASE_KERNEL(fused_diffusion_kernel);
    RELEASE_KERNEL(fused_diffusion_kernel_fast);
    RELEASE_KERNEL(conv2d_forward_kernel);
    RELEASE_KERNEL(conv2d_forward_kernel_fast);
    RELEASE_KERNEL(conv2d_backward_input_kernel);
    RELEASE_KERNEL(conv2d_backward_input_kernel_fast);
    RELEASE_KERNEL(conv2d_backward_weight_kernel);
    RELEASE_KERNEL(conv2d_backward_weight_kernel_fast);
    RELEASE_KERNEL(conv2d_bias_grad_kernel);
    RELEASE_KERNEL(conv2d_bias_grad_kernel_fast);
    RELEASE_KERNEL(patch_permute_kernel);
    RELEASE_KERNEL(patch_permute_kernel_fast);
    RELEASE_KERNEL(patch_permute_backward_kernel);
    RELEASE_KERNEL(patch_permute_backward_kernel_fast);
    RELEASE_KERNEL(izhikevich_kernel);
    RELEASE_KERNEL(izhikevich_kernel_fast);
    RELEASE_KERNEL(stdp_update_kernel);
    RELEASE_KERNEL(stdp_update_kernel_fast);
    RELEASE_KERNEL(stdp_trace_kernel);
    RELEASE_KERNEL(stdp_trace_kernel_fast);
    RELEASE_KERNEL(lbm_kernel);
    RELEASE_KERNEL(lbm_kernel_fast);
    RELEASE_KERNEL(nbody_forces_kernel);
    RELEASE_KERNEL(nbody_forces_kernel_fast);
    RELEASE_KERNEL(nbody_integrate_kernel);
    RELEASE_KERNEL(nbody_integrate_kernel_fast);
    RELEASE_KERNEL(ising_kernel);
    RELEASE_KERNEL(ising_kernel_fast);
    RELEASE_KERNEL(hebbian_update_local_reduce_kernel);
    RELEASE_KERNEL(hebbian_update_local_reduce_kernel_fast);
    RELEASE_KERNEL(embedding_backward_calc_delta_local_kernel);
    RELEASE_KERNEL(embedding_backward_calc_delta_local_kernel_fast);
    RELEASE_KERNEL(proto_segmented_sum_kernel);
    RELEASE_KERNEL(proto_segmented_sum_kernel_fast);
    RELEASE_KERNEL(proto_update_step_kernel);
    RELEASE_KERNEL(proto_update_step_kernel_fast);
    RELEASE_KERNEL(shape_loss_reward_penalty_kernel);
    RELEASE_KERNEL(shape_loss_reward_penalty_kernel_fast);
    RELEASE_KERNEL(shape_loss_reward_penalty_list_kernel);
    RELEASE_KERNEL(shape_loss_reward_penalty_list_kernel_fast); // NEU
    RELEASE_KERNEL(subqg_simulation_kernel);
    RELEASE_KERNEL(subqg_simulation_kernel_fast);
    RELEASE_KERNEL(subqg_agent_kernel);
    RELEASE_KERNEL(mycel_reinforce_kernel);
    RELEASE_KERNEL(mycel_diffuse_kernel);
    RELEASE_KERNEL(mycel_nutrient_kernel);
    RELEASE_KERNEL(render_kernel_img);
    RELEASE_KERNEL(render_kernel_buf);
    RELEASE_KERNEL(render_debug_kernel);
    RELEASE_KERNEL(sqse_encrypt_kernel);
    RELEASE_KERNEL(sqse_decrypt_kernel);
    RELEASE_KERNEL(quantum_single_qubit_kernel);
    RELEASE_KERNEL(quantum_controlled_phase_kernel);
    RELEASE_KERNEL(quantum_controlled_not_kernel);
    RELEASE_KERNEL(quantum_phase_oracle_kernel);
    RELEASE_KERNEL(quantum_phase_zero_kernel);
    RELEASE_KERNEL(quantum_modexp_kernel);
    RELEASE_KERNEL(quantum_swap_kernel);
    RELEASE_KERNEL(quantum_probability_kernel);
    RELEASE_KERNEL(quantum_expectation_pauli_z_kernel);
    RELEASE_KERNEL(quantum_apply_gate_kernel);
    RELEASE_KERNEL(quantum_vqe_gradient_kernel);
    RELEASE_KERNEL(qualia_resonator_kernel);
    RELEASE_KERNEL(intuition_precognition_kernel);
    RELEASE_KERNEL(context_resonance_kernel);
    RELEASE_KERNEL(dream_state_generator_kernel);
    RELEASE_KERNEL(transformation_planner_kernel);
    RELEASE_KERNEL(system_narrative_kernel);
    RELEASE_KERNEL(symbolic_abstraction_kernel);
    #undef RELEASE_KERNEL
    printf("[C] shutdown_driver: Kernels released.\n");

    // Release all program objects
    #define RELEASE_PROGRAM(p) if (p) { clReleaseProgram(p); p = NULL; }
    RELEASE_PROGRAM(matmul_program);
    RELEASE_PROGRAM(matmul_program_fast);
    RELEASE_PROGRAM(softmax_program);
    RELEASE_PROGRAM(softmax_program_fast);
    RELEASE_PROGRAM(gelu_program);
    RELEASE_PROGRAM(gelu_program_fast);
    RELEASE_PROGRAM(add_program);
    RELEASE_PROGRAM(add_program_fast);
    RELEASE_PROGRAM(mul_program);
    RELEASE_PROGRAM(mul_program_fast);
    RELEASE_PROGRAM(layernorm_program);
    RELEASE_PROGRAM(layernorm_program_fast);
    RELEASE_PROGRAM(transpose_program);
    RELEASE_PROGRAM(transpose_program_fast);
    RELEASE_PROGRAM(gelu_backward_program);
    RELEASE_PROGRAM(gelu_backward_program_fast);
    RELEASE_PROGRAM(matmul_backward_da_program);
    RELEASE_PROGRAM(matmul_backward_da_program_fast);
    RELEASE_PROGRAM(matmul_backward_db_program);
    RELEASE_PROGRAM(matmul_backward_db_program_fast);
    RELEASE_PROGRAM(layernorm_backward_program);
    RELEASE_PROGRAM(layernorm_backward_program_fast);
    RELEASE_PROGRAM(adam_program);
    RELEASE_PROGRAM(adam_program_fast);
    RELEASE_PROGRAM(softmax_backward_program);
    RELEASE_PROGRAM(softmax_backward_program_fast);
    RELEASE_PROGRAM(mul_backward_program);
    RELEASE_PROGRAM(mul_backward_program_fast);
    RELEASE_PROGRAM(transpose_backward_program);
    RELEASE_PROGRAM(transpose_backward_program_fast);
    RELEASE_PROGRAM(embedding_lookup_program);
    RELEASE_PROGRAM(embedding_lookup_program_fast);
    RELEASE_PROGRAM(reduce_sum_program);
    RELEASE_PROGRAM(reduce_sum_program_fast);
    RELEASE_PROGRAM(broadcast_add_program);
    RELEASE_PROGRAM(broadcast_add_program_fast);
    RELEASE_PROGRAM(transpose_batched_program);
    RELEASE_PROGRAM(transpose_batched_program_fast);
    RELEASE_PROGRAM(transpose_12_batched_program);
    RELEASE_PROGRAM(transpose_12_batched_program_fast);
    RELEASE_PROGRAM(matmul_batched_program);
    RELEASE_PROGRAM(matmul_batched_program_fast);
    RELEASE_PROGRAM(matmul_batched_backward_da_program);
    RELEASE_PROGRAM(matmul_batched_backward_da_program_fast);
    RELEASE_PROGRAM(matmul_batched_backward_db_program);
    RELEASE_PROGRAM(matmul_batched_backward_db_program_fast);
    RELEASE_PROGRAM(log_softmax_program);
    RELEASE_PROGRAM(log_softmax_program_fast);
    RELEASE_PROGRAM(cross_entropy_program);
    RELEASE_PROGRAM(cross_entropy_program_fast);
    RELEASE_PROGRAM(add_broadcast_pe_program);
    RELEASE_PROGRAM(add_broadcast_pe_program_fast);
    RELEASE_PROGRAM(threshold_spike_program);
    RELEASE_PROGRAM(threshold_spike_program_fast);
    RELEASE_PROGRAM(add_bias_mn_program);
    RELEASE_PROGRAM(add_bias_mn_program_fast);
    RELEASE_PROGRAM(dynamic_token_assign_program);
    RELEASE_PROGRAM(dynamic_token_assign_program_fast);
    RELEASE_PROGRAM(pairwise_similarity_program);
    RELEASE_PROGRAM(pairwise_similarity_program_fast);
    RELEASE_PROGRAM(fused_diffusion_program);
    RELEASE_PROGRAM(fused_diffusion_program_fast);
    RELEASE_PROGRAM(conv2d_forward_program);
    RELEASE_PROGRAM(conv2d_forward_program_fast);
    RELEASE_PROGRAM(conv2d_backward_input_program);
    RELEASE_PROGRAM(conv2d_backward_input_program_fast);
    RELEASE_PROGRAM(conv2d_backward_weight_program);
    RELEASE_PROGRAM(conv2d_backward_weight_program_fast);
    RELEASE_PROGRAM(conv2d_bias_grad_program);
    RELEASE_PROGRAM(conv2d_bias_grad_program_fast);
    RELEASE_PROGRAM(patch_permute_program);
    RELEASE_PROGRAM(patch_permute_program_fast);
    RELEASE_PROGRAM(patch_permute_backward_program);
    RELEASE_PROGRAM(patch_permute_backward_program_fast);
    RELEASE_PROGRAM(izhikevich_program);
    RELEASE_PROGRAM(izhikevich_program_fast);
    RELEASE_PROGRAM(stdp_update_program);
    RELEASE_PROGRAM(stdp_update_program_fast);
    RELEASE_PROGRAM(stdp_trace_program);
    RELEASE_PROGRAM(stdp_trace_program_fast);
    RELEASE_PROGRAM(lbm_program);
    RELEASE_PROGRAM(lbm_program_fast);
    RELEASE_PROGRAM(nbody_forces_program);
    RELEASE_PROGRAM(nbody_forces_program_fast);
    RELEASE_PROGRAM(nbody_integrate_program);
    RELEASE_PROGRAM(nbody_integrate_program_fast);
    RELEASE_PROGRAM(ising_program);
    RELEASE_PROGRAM(ising_program_fast);
    RELEASE_PROGRAM(hebbian_update_local_reduce_program);
    RELEASE_PROGRAM(hebbian_update_local_reduce_program_fast);
    RELEASE_PROGRAM(embedding_backward_calc_delta_local_program);
    RELEASE_PROGRAM(embedding_backward_calc_delta_local_program_fast);
    RELEASE_PROGRAM(proto_segmented_sum_program);
    RELEASE_PROGRAM(proto_segmented_sum_program_fast);
    RELEASE_PROGRAM(proto_update_step_program);
    RELEASE_PROGRAM(proto_update_step_program_fast);
    RELEASE_PROGRAM(shape_loss_reward_penalty_program);
    RELEASE_PROGRAM(shape_loss_reward_penalty_program_fast);
    RELEASE_PROGRAM(shape_loss_reward_penalty_list_program);
    RELEASE_PROGRAM(shape_loss_reward_penalty_list_program_fast); // NEU
    RELEASE_PROGRAM(subqg_simulation_program);
    RELEASE_PROGRAM(subqg_simulation_program_fast);
    RELEASE_PROGRAM(subqg_agent_program);
    RELEASE_PROGRAM(mycel_program);
    RELEASE_PROGRAM(render_program);
    RELEASE_PROGRAM(sqse_program);
    RELEASE_PROGRAM(quantum_program);
    #undef RELEASE_PROGRAM
    printf("[C] shutdown_driver: Programs released.\n");

    // Release SubQG buffers/state
    release_subqg_resources();
    release_quantum_resources();

    // Finish pending commands and release queue
    if (queue) {
        cl_int finish_err = clFinish(queue);
        if(finish_err != CL_SUCCESS) {
            fprintf(stderr, "[C] shutdown_driver: Warning - clFinish failed before releasing queue: %s (%d)\n", clGetErrorString(finish_err), finish_err);
        }
        clReleaseCommandQueue(queue);
        queue = NULL;
        printf("[C] shutdown_driver: Command queue released.\n");
    }

    // Release context
    if (context) {
        clReleaseContext(context);
        context = NULL;
        printf("[C] shutdown_driver: Context released.\n");
    }

    cc_release_all_slots();

    // Reset device/platform handles and flags
    device_id = NULL;
    platform_id = NULL;
    has_fp64_support = 0;
    has_atomics_support = 0;

    printf("[C] shutdown_driver: Cleanup finished.\n");
}

/**
 * @brief Queries and returns the number of compute units (CUs) on the selected device.
 */
unsigned int get_compute_unit_count(int gpu_index) {
    if (!device_id) { return 0; }
    cl_uint cu_count = 0;
    cl_int err = clGetDeviceInfo(device_id, CL_DEVICE_MAX_COMPUTE_UNITS, sizeof(cl_uint), &cu_count, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] get_compute_unit_count: clGetDeviceInfo failed for CL_DEVICE_MAX_COMPUTE_UNITS: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return (unsigned int)cu_count;
}

static void cc_reset_slot(GpuSlot* slot) {
    if (!slot) { return; }
    if (slot->pinned_amp_host && slot->pinned_amp_buffer && slot->queue) {
        cl_int unmap_err = clEnqueueUnmapMemObject(slot->queue, slot->pinned_amp_buffer,
                                                   slot->pinned_amp_host, 0, NULL, NULL);
        if (unmap_err == CL_SUCCESS) {
            clFinish(slot->queue);
        }
    }
    if (slot->pinned_amp_buffer) {
        clReleaseMemObject(slot->pinned_amp_buffer);
    }
    if (slot->owns_objects) {
        if (slot->program) {
            clReleaseProgram(slot->program);
        }
        if (slot->queue) {
            clReleaseCommandQueue(slot->queue);
        }
        if (slot->transfer_queue && slot->transfer_queue != slot->queue) {
            clReleaseCommandQueue(slot->transfer_queue);
        }
        if (slot->context) {
            clReleaseContext(slot->context);
        }
    }
    memset(slot, 0, sizeof(*slot));
}

static int cc_discover_devices_once(void) {
    cc_lock_init_once();
    CC_LOCK();
    if (g_slot_count_discovered >= 0) {
        int count = g_slot_count_discovered;
        CC_UNLOCK();
        return count;
    }

    memset(g_gpu_slots, 0, sizeof(g_gpu_slots));
    cl_uint num_platforms = 0;
    cl_int err = clGetPlatformIDs(0, NULL, &num_platforms);
    if (err != CL_SUCCESS || num_platforms == 0) {
        fprintf(stderr, "[C] GPU Manager: Failed to query OpenCL platforms: %s (%d)\n", clGetErrorString(err), err);
        g_slot_count_discovered = 0;
        CC_UNLOCK();
        return 0;
    }

    cl_platform_id platforms[CC_MAX_DEVICES] = {0};
    if (num_platforms > CC_MAX_DEVICES) {
        num_platforms = CC_MAX_DEVICES;
    }
    err = clGetPlatformIDs(num_platforms, platforms, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] GPU Manager: Failed to enumerate platform IDs: %s (%d)\n", clGetErrorString(err), err);
        g_slot_count_discovered = 0;
        CC_UNLOCK();
        return 0;
    }

    int slot_idx = 0;
    for (cl_uint p = 0; p < num_platforms && slot_idx < CC_MAX_DEVICES; ++p) {
        cl_uint num_devices = 0;
        err = clGetDeviceIDs(platforms[p], CL_DEVICE_TYPE_GPU, 0, NULL, &num_devices);
        if (err != CL_SUCCESS || num_devices == 0) {
            continue;
        }

        cl_device_id devices[2 * CC_MAX_DEVICES] = {0};
        if (num_devices > (cl_uint)(2 * CC_MAX_DEVICES)) {
            num_devices = (cl_uint)(2 * CC_MAX_DEVICES);
        }
        err = clGetDeviceIDs(platforms[p], CL_DEVICE_TYPE_GPU, num_devices, devices, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] GPU Manager: Failed to enumerate devices for platform %u: %s (%d)\n", p, clGetErrorString(err), err);
            continue;
        }

        for (cl_uint d = 0; d < num_devices && slot_idx < CC_MAX_DEVICES; ++d) {
            g_gpu_slots[slot_idx].platform = platforms[p];
            g_gpu_slots[slot_idx].device = devices[d];
            g_gpu_slots[slot_idx].initialized = 0;
            g_gpu_slots[slot_idx].in_error = 0;
            ++slot_idx;
        }
    }

    g_slot_count_discovered = slot_idx;
    if (slot_idx == 0) {
        fprintf(stderr, "[C] GPU Manager: No GPU devices discovered across available platforms.\n");
    }
    CC_UNLOCK();
    return g_slot_count_discovered;
}

static void cc_mark_slot_initialized(int gpu_index, cl_context ctx, cl_command_queue q, cl_program program) {
    if (gpu_index < 0 || gpu_index >= CC_MAX_DEVICES) { return; }
    cc_lock_init_once();
    CC_LOCK();
    GpuSlot* slot = &g_gpu_slots[gpu_index];
    slot->context = ctx;
    slot->queue = q;
    slot->transfer_queue = q;
    slot->program = program;
    slot->initialized = (ctx && q) ? 1 : 0;
    slot->in_error = (ctx && q) ? 0 : 1;
    slot->owns_objects = 0;
    slot->out_of_order_enabled = 0;
    slot->pinned_amp_buffer = NULL;
    slot->pinned_amp_host = NULL;
    slot->pinned_amp_bytes = 0;
    CC_UNLOCK();
}

static int cc_initialize_slot_resources(int gpu_index, GpuSlot* slot) {
    if (!slot) { return 0; }
    cl_int err = CL_SUCCESS;
    cl_context_properties props[] = {
        CL_CONTEXT_PLATFORM, (cl_context_properties)slot->platform,
        0
    };
    cl_context ctx = clCreateContext(props, 1, &slot->device, NULL, NULL, &err);
    if (err != CL_SUCCESS || !ctx) {
        fprintf(stderr, "[C] GPU Manager: Failed to create context for slot %d: %s (%d)\n",
                gpu_index, clGetErrorString(err), err);
        return 0;
    }

    cl_command_queue main_queue = NULL;
    cl_command_queue transfer_queue = NULL;
    cl_int out_of_order = 0;
#if defined(CL_VERSION_2_0)
    const cl_queue_properties queue_props[] = {
        CL_QUEUE_PROPERTIES, CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE | CL_QUEUE_PROFILING_ENABLE,
        0
    };
    main_queue = clCreateCommandQueueWithProperties(ctx, slot->device, queue_props, &err);
    if (err == CL_SUCCESS && main_queue) {
        out_of_order = 1;
    } else {
        const cl_queue_properties queue_props_inorder[] = {
            CL_QUEUE_PROPERTIES, CL_QUEUE_PROFILING_ENABLE,
            0
        };
        err = CL_SUCCESS;
        main_queue = clCreateCommandQueueWithProperties(ctx, slot->device, queue_props_inorder, &err);
    }
#else
    cl_command_queue_properties queue_props = CL_QUEUE_PROFILING_ENABLE | CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE;
    main_queue = clCreateCommandQueue(ctx, slot->device, queue_props, &err);
    if (err != CL_SUCCESS || !main_queue) {
        queue_props = CL_QUEUE_PROFILING_ENABLE;
        err = CL_SUCCESS;
        main_queue = clCreateCommandQueue(ctx, slot->device, queue_props, &err);
    } else {
        out_of_order = 1;
    }
#endif
    if (err != CL_SUCCESS || !main_queue) {
        fprintf(stderr, "[C] GPU Manager: Failed to create command queue for slot %d: %s (%d)\n",
                gpu_index, clGetErrorString(err), err);
        clReleaseContext(ctx);
        return 0;
    }

#if defined(CL_VERSION_2_0)
    if (out_of_order) {
        const cl_queue_properties transfer_props[] = {
            CL_QUEUE_PROPERTIES, CL_QUEUE_PROFILING_ENABLE,
            0
        };
        cl_int transfer_err = CL_SUCCESS;
        transfer_queue = clCreateCommandQueueWithProperties(ctx, slot->device, transfer_props, &transfer_err);
        if (transfer_err != CL_SUCCESS || !transfer_queue) {
            transfer_queue = main_queue;
        }
    } else {
        transfer_queue = main_queue;
    }
#else
    transfer_queue = main_queue;
#endif

    cl_mem pinned = NULL;
    cl_float2* host_ptr = NULL;
    size_t pinned_bytes = CC_PINNED_STAGING_MIN_BYTES;
    pinned = clCreateBuffer(ctx, CL_MEM_READ_WRITE | CL_MEM_ALLOC_HOST_PTR, pinned_bytes, NULL, &err);
    if (err == CL_SUCCESS && pinned) {
        host_ptr = (cl_float2*)clEnqueueMapBuffer(main_queue, pinned, CL_TRUE,
                                                  CL_MAP_READ | CL_MAP_WRITE,
                                                  0, pinned_bytes, 0, NULL, NULL, &err);
        if (err != CL_SUCCESS || !host_ptr) {
            clReleaseMemObject(pinned);
            pinned = NULL;
            host_ptr = NULL;
        }
    } else {
        pinned = NULL;
    }

    slot->context = ctx;
    slot->queue = main_queue;
    slot->transfer_queue = transfer_queue;
    slot->program = NULL;
    slot->initialized = 1;
    slot->in_error = 0;
    slot->owns_objects = 1;
    slot->out_of_order_enabled = out_of_order;
    slot->pinned_amp_buffer = pinned;
    slot->pinned_amp_host = host_ptr;
    slot->pinned_amp_bytes = pinned ? pinned_bytes : 0;
    return 1;
}

static int cc_ensure_slot_initialized(int gpu_index) {
    if (gpu_index < 0) { gpu_index = 0; }

    cc_lock_init_once();
    CC_LOCK();
    GpuSlot* slot = &g_gpu_slots[gpu_index];
    if (slot->initialized && !slot->in_error) {
        CC_UNLOCK();
        return 1;
    }
    CC_UNLOCK();

    cc_lock_init_once();
    CC_LOCK();
    slot = &g_gpu_slots[gpu_index];
    if (slot->initialized && !slot->in_error) {
        CC_UNLOCK();
        return 1;
    }
    if (!slot->platform || !slot->device) {
        CC_UNLOCK();
        fprintf(stderr, "[C] GPU Manager: Slot %d missing platform/device information.\n", gpu_index);
        return 0;
    }
    CC_UNLOCK();

    if (!cc_initialize_slot_resources(gpu_index, slot)) {
        cc_lock_init_once();
        CC_LOCK();
        slot->in_error = 1;
        CC_UNLOCK();
        return 0;
    }

    cc_lock_init_once();
    CC_LOCK();
    slot->initialized = 1;
    slot->in_error = 0;
    CC_UNLOCK();

    if (!context || !queue) {
        context = slot->context;
        queue = slot->queue;
        device_id = slot->device;
        platform_id = slot->platform;
    }
    return 1;
}

static GpuSlot* cc_get_slot(int gpu_index) {
    if (gpu_index < 0) { gpu_index = 0; }
    int available = cc_discover_devices_once();
    if (available <= 0 || gpu_index >= available) {
        return NULL;
    }

    if (!cc_ensure_slot_initialized(gpu_index)) {
        return NULL;
    }

    cc_lock_init_once();
    CC_LOCK();
    GpuSlot* slot = &g_gpu_slots[gpu_index];
    if (!slot->initialized || slot->in_error) {
        CC_UNLOCK();
        return NULL;
    }
    CC_UNLOCK();
    return slot;
}

static void cc_release_all_slots(void) {
    cc_lock_init_once();
    CC_LOCK();
    for (int i = 0; i < CC_MAX_DEVICES; ++i) {
        cc_reset_slot(&g_gpu_slots[i]);
    }
    g_slot_count_discovered = -1;
    CC_UNLOCK();
}

// --- Exported Functions ---

/**
 * @brief Initializes the OpenCL environment for a specific GPU.
 */
DLLEXPORT void set_quantum_enabled(int enabled) {
    g_quantum_enabled = enabled ? 1 : 0;
    if (!g_quantum_enabled) {
        release_quantum_program_objects();
        release_quantum_resources();
        g_quantum_disabled_warned = 0;
    } else {
        g_quantum_disabled_warned = 0;
    }
}

DLLEXPORT int initialize_gpu(int gpu_index) {
    cl_int err;

    // Prevent re-initialization
    if (context || queue || device_id) {
         fprintf(stderr, "[C] initialize_gpu: Warning - Already initialized. Re-initialization attempt for index %d ignored.\n", gpu_index);
         return 1;
    }

    cc_discover_devices_once();

    if (cc_env_quantum_disabled()) {
        g_quantum_enabled = 0;
    }

    printf("[C] initialize_gpu: Initializing OpenCL for GPU index %d...\n", gpu_index);

    // --- Find Platform ---
    cl_uint num_platforms;
    err = clGetPlatformIDs(0, NULL, &num_platforms);
    if (err != CL_SUCCESS || num_platforms == 0) {
        fprintf(stderr, "[C] initialize_gpu: Error - No OpenCL platforms found (%s, num=%u).\n", clGetErrorString(err), num_platforms);
        return 0;
    }

    cl_platform_id* platforms = (cl_platform_id*)malloc(num_platforms * sizeof(cl_platform_id));
    if (!platforms) {
        fprintf(stderr, "[C] initialize_gpu: Error - Failed to allocate memory for platform IDs.\n");
        return 0;
    }
    err = clGetPlatformIDs(num_platforms, platforms, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] initialize_gpu: Error getting platform IDs: %s (%d)\n", clGetErrorString(err), err);
        free(platforms);
        return 0;
    }
    // Wähle erste Plattform mit mindestens einem GPU-Device
    platform_id = NULL;
    for (cl_uint pi = 0; pi < num_platforms; ++pi) {
        cl_uint nd = 0;
        if (clGetDeviceIDs(platforms[pi], CL_DEVICE_TYPE_GPU, 0, NULL, &nd) == CL_SUCCESS && nd > 0) {
            platform_id = platforms[pi];
            break;
        }
    }
    // Fallback: wenn keine Plattform GPUs hat, nimm platforms[0] (restlicher Code behandelt CL_DEVICE_TYPE_ALL)
    if (!platform_id) {
        platform_id = platforms[0];
    }
    free(platforms);

    char platformName[1024] = {0};
    clGetPlatformInfo(platform_id, CL_PLATFORM_NAME, sizeof(platformName)-1, platformName, NULL);
    printf("[C] initialize_gpu: Using platform: %s\n", platformName);

    // --- Find Device ---
    cl_uint num_devices;
    cl_device_type selected_device_type = CL_DEVICE_TYPE_GPU;
    err = clGetDeviceIDs(platform_id, selected_device_type, 0, NULL, &num_devices);
    if (err != CL_SUCCESS || num_devices == 0) {
        fprintf(stderr, "[C] initialize_gpu: No GPU devices found on platform '%s'. Trying CL_DEVICE_TYPE_ALL...\n", platformName);
        selected_device_type = CL_DEVICE_TYPE_ALL;
        err = clGetDeviceIDs(platform_id, selected_device_type, 0, NULL, &num_devices);
        if(err != CL_SUCCESS || num_devices == 0) {
            fprintf(stderr, "[C] initialize_gpu: Error - No OpenCL devices found at all on platform '%s'.\n", platformName);
            return 0;
        }
        printf("[C] initialize_gpu: Found %u devices of type CL_DEVICE_TYPE_ALL.\n", num_devices);
    } else {
        printf("[C] initialize_gpu: Found %u GPU devices.\n", num_devices);
    }

    if (gpu_index < 0 || gpu_index >= (int)num_devices) {
        fprintf(stderr, "[C] initialize_gpu: Error - gpu_index=%d out of range [0, %d).\n", gpu_index, (int)num_devices);
        // Verfügbare Geräte auflisten
        cl_device_id* tmp = (cl_device_id*)malloc(num_devices * sizeof(cl_device_id));
        if (tmp) {
            if (clGetDeviceIDs(platform_id, selected_device_type, num_devices, tmp, NULL) == CL_SUCCESS) {
                for (cl_uint di = 0; di < num_devices; ++di) {
                    char name[256] = {0};
                    clGetDeviceInfo(tmp[di], CL_DEVICE_NAME, sizeof(name)-1, name, NULL);
                    fprintf(stderr, "    [GPU %u] %s\n", di, name);
                }
            }
            free(tmp);
        }
        return 0;
    }

    cl_device_id* devices = (cl_device_id*)malloc(num_devices * sizeof(cl_device_id));
    if (!devices) {
        fprintf(stderr, "[C] initialize_gpu: Error - Failed to allocate memory for device IDs.\n");
        return 0;
    }
    err = clGetDeviceIDs(platform_id, selected_device_type, num_devices, devices, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] initialize_gpu: Error - Failed to get device IDs: %s (%d)\n", clGetErrorString(err), err);
        free(devices);
        return 0;
    }
    device_id = devices[gpu_index];
    free(devices);

    cc_lock_init_once();
    CC_LOCK();
    if (gpu_index >= 0 && gpu_index < CC_MAX_DEVICES) {
        g_gpu_slots[gpu_index].platform = platform_id;
        g_gpu_slots[gpu_index].device = device_id;
    }
    CC_UNLOCK();

    char deviceName[1024] = {0};
    clGetDeviceInfo(device_id, CL_DEVICE_NAME, sizeof(deviceName)-1, deviceName, NULL);
    printf("[C] initialize_gpu: Using device index %d: %s\n", gpu_index, deviceName);

    // --- Check Device Capabilities ---
    cl_device_fp_config fp_config;
    err = clGetDeviceInfo(device_id, CL_DEVICE_DOUBLE_FP_CONFIG, sizeof(fp_config), &fp_config, NULL);
    has_fp64_support = (err == CL_SUCCESS && (fp_config & CL_FP_FMA));
    printf("[C] initialize_gpu: FP64 Support (CL_FP_FMA flag): %s\n", has_fp64_support ? "Yes" : "No");

    has_atomics_support = 0;
    has_int64_atomics = 0;
    int has_int32_atomics = 0;
    char* extensions_str = NULL;
    size_t extensions_size = 0;
    err = clGetDeviceInfo(device_id, CL_DEVICE_EXTENSIONS, 0, NULL, &extensions_size);
    if (err == CL_SUCCESS && extensions_size > 1) {
        extensions_str = (char*)malloc(extensions_size);
        if (extensions_str) {
            err = clGetDeviceInfo(device_id, CL_DEVICE_EXTENSIONS, extensions_size, extensions_str, NULL);
            if (err == CL_SUCCESS) {
                if (strstr(extensions_str, "cl_khr_global_int32_base_atomics") != NULL) {
                    printf("[C] initialize_gpu: Found 'cl_khr_global_int32_base_atomics'. Basic 32-bit global atomics SUPPORTED.\n");
                    has_int32_atomics = 1;
                    if (strstr(extensions_str, "cl_khr_int64_base_atomics") != NULL) {
                        printf("[C] initialize_gpu: Found 'cl_khr_int64_base_atomics'. 64-bit atomics SUPPORTED (preferred for float CAS).\n");
                        has_int64_atomics = 1;
                    } else {
                        printf("[C] initialize_gpu: WARNING - 64-bit atomics missing. Falling back to 32-bit CAS for atomic_add_float (may introduce accumulation jitter).\n");
                    }
                } else {
                    printf("[C] initialize_gpu: Extension 'cl_khr_global_int32_base_atomics' NOT FOUND. GPU Proto Update (segmented sum) will FAIL if attempted.\n");
                }
            } else {
                fprintf(stderr, "[C] initialize_gpu: Warning - Failed to query CL_DEVICE_EXTENSIONS content: %s (%d)\n", clGetErrorString(err), err);
            }
            free(extensions_str); extensions_str = NULL;
        } else {
            fprintf(stderr, "[C] initialize_gpu: Warning - Failed to allocate memory (%zu bytes) for extensions string.\n", extensions_size);
        }
    } else {
        fprintf(stderr, "[C] initialize_gpu: Warning - Failed to query CL_DEVICE_EXTENSIONS size or size is trivial: %s (%d), size=%zu\n", clGetErrorString(err), err, extensions_size);
    }
    has_atomics_support = has_int32_atomics;
    printf("[C] initialize_gpu: Atomics Support Flag (has_atomics_support): %d\n", has_atomics_support);


    // --- Create Context ---
    context = clCreateContext(NULL, 1, &device_id, NULL, NULL, &err);
    if (!context || err != CL_SUCCESS) {
        fprintf(stderr, "[C] initialize_gpu: clCreateContext failed: %s (%d)\n", clGetErrorString(err), err);
        shutdown_driver();
        return 0;
    }
    printf("[C] initialize_gpu: Context created.\n");

    // --- Create Command Queue ---
    #if CL_TARGET_OPENCL_VERSION >= 200
        const cl_queue_properties queue_props[] = {
            CL_QUEUE_PROPERTIES, (cl_queue_properties)CL_QUEUE_PROFILING_ENABLE,
            0
        };
        queue = clCreateCommandQueueWithProperties(context, device_id, queue_props, &err);
        if (!queue || err != CL_SUCCESS) {
            fprintf(stderr, "[C] initialize_gpu: clCreateCommandQueueWithProperties failed: %s (%d). Trying deprecated clCreateCommandQueue...\n", clGetErrorString(err), err);
            #if defined(__GNUC__) || defined(__clang__)
            #pragma GCC diagnostic push
            #pragma GCC diagnostic ignored "-Wdeprecated-declarations"
            #endif
            #ifdef _MSC_VER
            #pragma warning(push)
            #pragma warning(disable: 4996)
            #endif
            queue = clCreateCommandQueue(context, device_id, CL_QUEUE_PROFILING_ENABLE, &err);
            #ifdef _MSC_VER
            #pragma warning(pop)
            #endif
            #if defined(__GNUC__) || defined(__clang__)
            #pragma GCC diagnostic pop
            #endif
        }
    #else
        #if defined(__GNUC__) || defined(__clang__)
        #pragma GCC diagnostic push
        #pragma GCC diagnostic ignored "-Wdeprecated-declarations"
        #endif
        #ifdef _MSC_VER
        #pragma warning(push)
        #pragma warning(disable: 4996)
        #endif
        queue = clCreateCommandQueue(context, device_id, CL_QUEUE_PROFILING_ENABLE, &err);
        #ifdef _MSC_VER
        #pragma warning(pop)
        #endif
        #if defined(__GNUC__) || defined(__clang__)
        #pragma GCC diagnostic pop
        #endif
    #endif

    if (!queue || err != CL_SUCCESS) {
        fprintf(stderr, "[C] initialize_gpu: Failed to create command queue: %s (%d)\n", clGetErrorString(err), err);
        shutdown_driver();
        return 0;
    }
    printf("[C] initialize_gpu: Command queue created.\n");

    // Cache handles for the slot manager so later APIs can resolve the queue/context by gpu_index.
    cc_mark_slot_initialized(gpu_index, context, queue, NULL);

    // --- Compile All Kernels ---
    printf("[C] initialize_gpu: Compiling ALL OpenCL kernels...\n");
    cl_int compile_err;
    #define COMPILE_KERNEL_DUAL(src, name, base) \
        printf("[C] initialize_gpu: Compiling kernel '%s' (strict/fast)...\n", name); \
        compile_err = compile_opencl_kernel_dual(src, name, &base##_program, &base##_kernel, \
                                                &base##_program_fast, &base##_kernel_fast); \
        if (compile_err != CL_SUCCESS) { \
            fprintf(stderr, "[C] initialize_gpu: FATAL ERROR - Failed to compile kernel '%s'. Shutting down.\n", name); \
            shutdown_driver(); \
            return 0; \
        }

    // Compile each kernel
    COMPILE_KERNEL_DUAL(matmul_kernel_src, "matrix_multiply", matmul);
    COMPILE_KERNEL_DUAL(softmax_kernel_src, "softmax_rowwise", softmax);
    COMPILE_KERNEL_DUAL(gelu_kernel_src, "gelu_elementwise", gelu);
    COMPILE_KERNEL_DUAL(add_kernel_src, "add_elementwise", add);
    COMPILE_KERNEL_DUAL(mul_kernel_src, "mul_elementwise", mul);
    COMPILE_KERNEL_DUAL(layernorm_kernel_src, "layer_norm", layernorm);
    COMPILE_KERNEL_DUAL(transpose_kernel_src, "transpose", transpose);
    COMPILE_KERNEL_DUAL(gelu_backward_kernel_src, "gelu_backward_elementwise", gelu_backward);
    COMPILE_KERNEL_DUAL(matmul_backward_dA_kernel_src, "matmul_backward_da", matmul_backward_da);
    COMPILE_KERNEL_DUAL(matmul_backward_dB_kernel_src, "matmul_backward_db", matmul_backward_db);
    COMPILE_KERNEL_DUAL(layernorm_backward_kernel_src, "layer_norm_backward", layernorm_backward);
    COMPILE_KERNEL_DUAL(adam_kernel_src, "adam_update", adam);
    COMPILE_KERNEL_DUAL(softmax_backward_kernel_src, "softmax_backward", softmax_backward);
    // Note: Mul backward uses same program/kernel as forward Mul
    // COMPILE_KERNEL_DUAL(mul_backward_kernel_src, \"mul_backward\", mul_backward); // Uses mul_kernel
    COMPILE_KERNEL_DUAL(transpose_backward_kernel_src, "transpose_backward", transpose_backward);
    COMPILE_KERNEL_DUAL(embedding_lookup_kernel_src, "embedding_lookup", embedding_lookup);
    COMPILE_KERNEL_DUAL(reduce_sum_kernel_src, "reduce_sum_axis01", reduce_sum);
    COMPILE_KERNEL_DUAL(broadcast_add_kernel_src, "broadcast_add_bias", broadcast_add);
    COMPILE_KERNEL_DUAL(transpose_batched_kernel_src, "transpose_batched_last_two", transpose_batched);
    COMPILE_KERNEL_DUAL(transpose_12_batched_kernel_src, "transpose_12_batched", transpose_12_batched);
    COMPILE_KERNEL_DUAL(matmul_batched_kernel_src, "matmul_batched", matmul_batched);
    COMPILE_KERNEL_DUAL(matmul_batched_backward_dA_kernel_src, "matmul_batched_backward_da", matmul_batched_backward_da);
    COMPILE_KERNEL_DUAL(matmul_batched_backward_dB_kernel_src, "matmul_batched_backward_db", matmul_batched_backward_db);
    COMPILE_KERNEL_DUAL(log_softmax_stable_kernel_src, "log_softmax_stable_rowwise", log_softmax);
    COMPILE_KERNEL_DUAL(cross_entropy_loss_grad_kernel_src, "cross_entropy_loss_grad", cross_entropy);
    COMPILE_KERNEL_DUAL(add_broadcast_pe_kernel_src, "add_broadcast_pe", add_broadcast_pe);
    COMPILE_KERNEL_DUAL(threshold_spike_kernel_src, "threshold_spike", threshold_spike);
    COMPILE_KERNEL_DUAL(add_bias_mn_kernel_src, "add_bias_mn", add_bias_mn);
    COMPILE_KERNEL_DUAL(dynamic_token_assign_kernel_src, "dynamic_token_assignment", dynamic_token_assign);
    COMPILE_KERNEL_DUAL(pairwise_similarity_kernel_src, "pairwise_similarity_dot", pairwise_similarity);
    COMPILE_KERNEL_DUAL(fused_diffusion_kernel_src, "fused_diffusion", fused_diffusion);
    COMPILE_KERNEL_DUAL(conv2d_forward_kernel_src, "conv2d_forward", conv2d_forward);
    COMPILE_KERNEL_DUAL(conv2d_backward_input_kernel_src, "conv2d_backward_input", conv2d_backward_input);
    COMPILE_KERNEL_DUAL(conv2d_backward_weight_kernel_src, "conv2d_backward_weight", conv2d_backward_weight);
    COMPILE_KERNEL_DUAL(conv2d_bias_grad_kernel_src, "conv2d_bias_grad", conv2d_bias_grad);
    COMPILE_KERNEL_DUAL(patch_permute_kernel_src, "patch_permute_reshape", patch_permute);
    COMPILE_KERNEL_DUAL(patch_permute_backward_kernel_src, "patch_permute_reshape_backward", patch_permute_backward);
    COMPILE_KERNEL_DUAL(izhikevich_kernel_src, "izhikevich_neuron_step", izhikevich);
    COMPILE_KERNEL_DUAL(stdp_update_kernel_src, "stdp_update_step", stdp_update);
    COMPILE_KERNEL_DUAL(stdp_trace_kernel_src, "stdp_update_traces", stdp_trace);
    COMPILE_KERNEL_DUAL(lbm_kernel_src, "lbm_collide_and_stream", lbm);
    COMPILE_KERNEL_DUAL(nbody_forces_kernel_src, "nbody_calculate_forces", nbody_forces);
    COMPILE_KERNEL_DUAL(nbody_integrate_kernel_src, "nbody_integrate", nbody_integrate);
    COMPILE_KERNEL_DUAL(ising_kernel_src, "ising_metropolis_step", ising);
    COMPILE_KERNEL_DUAL(hebbian_update_local_reduce_kernel_src, "hebbian_update_local_reduce", hebbian_update_local_reduce);
    COMPILE_KERNEL_DUAL(embedding_backward_calc_delta_local_kernel_src, "embedding_backward_calc_delta_local", embedding_backward_calc_delta_local);
    COMPILE_KERNEL_DUAL(proto_segmented_sum_atomic_kernel_src, "proto_segmented_sum_atomic", proto_segmented_sum);
    COMPILE_KERNEL_DUAL(proto_update_step_kernel_src, "proto_update_step", proto_update_step);
    COMPILE_KERNEL_DUAL(shape_loss_reward_penalty_kernel_src, "shape_loss_reward_penalty", shape_loss_reward_penalty);
    // NEU: Compile Loss Shaping Kernel (List)
    COMPILE_KERNEL_DUAL(shape_loss_reward_penalty_list_kernel_src, "shape_loss_reward_penalty_list", shape_loss_reward_penalty_list);
    COMPILE_KERNEL_DUAL(subqg_simulation_kernel_src, "subqg_simulation_step", subqg_simulation);
    #undef COMPILE_KERNEL_DUAL
    printf("[C] initialize_gpu: Compiling kernel 'subqg_inject_agents'...\n");
    compile_err = compile_opencl_kernel_variant(subqg_agent_kernel_src, "subqg_inject_agents",
                                                &subqg_agent_program, &subqg_agent_kernel, 0);
    if (compile_err != CL_SUCCESS || !subqg_agent_kernel) {
        fprintf(stderr, "[C] initialize_gpu: Failed to compile subqg agent kernel: %s (%d)\n",
                clGetErrorString(compile_err), compile_err);
        shutdown_driver();
        return 0;
    }
    printf("[C] initialize_gpu: Compiling Mycel kernels...\n");
    compile_err = compile_opencl_kernel_variant(mycel_kernel_src, "mycel_reinforce",
                                                &mycel_program, &mycel_reinforce_kernel, 0);
    if (compile_err != CL_SUCCESS || !mycel_program || !mycel_reinforce_kernel) {
        fprintf(stderr, "[C] initialize_gpu: Failed to compile Mycel reinforcement kernel: %s (%d)\n",
                clGetErrorString(compile_err), compile_err);
        shutdown_driver();
        return 0;
    }
    cl_int mycel_err = CL_SUCCESS;
    mycel_diffuse_kernel = clCreateKernel(mycel_program, "mycel_diffuse_decay", &mycel_err);
    if (mycel_err != CL_SUCCESS || !mycel_diffuse_kernel) {
        fprintf(stderr, "[C] initialize_gpu: Failed to create Mycel diffusion kernel: %s (%d)\n",
                clGetErrorString(mycel_err), mycel_err);
        shutdown_driver();
        return 0;
    }
    mycel_nutrient_kernel = clCreateKernel(mycel_program, "mycel_nutrient_update", &mycel_err);
    if (mycel_err != CL_SUCCESS || !mycel_nutrient_kernel) {
        fprintf(stderr, "[C] initialize_gpu: Failed to create Mycel nutrient kernel: %s (%d)\n",
                clGetErrorString(mycel_err), mycel_err);
        shutdown_driver();
        return 0;
    }
    printf("[C] initialize_gpu: Compiling render kernel...\n");
    compile_err = compile_opencl_kernel_variant(render_kernel_src, "render_frame_img",
                                                &render_program, &render_kernel_img, 1);
    if (compile_err != CL_SUCCESS || !render_program || !render_kernel_img) {
        fprintf(stderr, "[C] initialize_gpu: Warning - Render kernel unavailable (%s, %d). GPU rendering disabled.\n",
                clGetErrorString(compile_err), compile_err);
        if (render_program) {
            clReleaseProgram(render_program);
        }
        render_program = NULL;
        render_kernel_img = NULL;
        render_kernel_buf = NULL;
        render_debug_kernel = NULL;
    } else {
        cl_int buf_err = CL_SUCCESS;
        render_kernel_buf = clCreateKernel(render_program, "render_frame_buf", &buf_err);
        if (buf_err != CL_SUCCESS || !render_kernel_buf) {
            fprintf(stderr, "[C] initialize_gpu: Warning - Failed to create render_frame_buf kernel: %s (%d)\n",
                    clGetErrorString(buf_err), buf_err);
            render_kernel_buf = NULL;
        }
        cl_int debug_err = CL_SUCCESS;
        render_debug_kernel = clCreateKernel(render_program, "render_debug", &debug_err);
        if (debug_err != CL_SUCCESS || !render_debug_kernel) {
            fprintf(stderr, "[C] initialize_gpu: Warning - Failed to create render_debug kernel: %s (%d)\n",
                    clGetErrorString(debug_err), debug_err);
            render_debug_kernel = NULL;
        } else {
            printf("[C] initialize_gpu: render_debug kernel ready.\n");
        }
    }
    printf("[C] initialize_gpu: Compiling SQSE kernels...\n");
    if (!ensure_sqse_kernels_ready()) {
        fprintf(stderr, "[C] initialize_gpu: Failed to compile SQSE kernels.\n");
        shutdown_driver();
        return 0;
    }
    if (!g_quantum_enabled) {
        printf("[C] initialize_gpu: Quantum kernels disabled via configuration. Skipping compilation.\n");
    } else {
        do {
            printf("[C] initialize_gpu: Compiling kernel 'quantum_apply_single_qubit' (strict only)...\n");
            compile_err = compile_opencl_kernel_variant(quantum_simulation_kernels_src, "quantum_apply_single_qubit",
                                                        &quantum_program, &quantum_single_qubit_kernel, 0);
            if (compile_err != CL_SUCCESS || !quantum_program || !quantum_single_qubit_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Quantum kernel base compilation failed. Disabling quantum features.\n");
                g_quantum_enabled = 0;
                break;
            }

            cl_int qerr = CL_SUCCESS;
            quantum_controlled_phase_kernel = clCreateKernel(quantum_program, "quantum_apply_controlled_phase", &qerr);
            if (qerr != CL_SUCCESS || !quantum_controlled_phase_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum controlled phase kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_controlled_not_kernel = clCreateKernel(quantum_program, "quantum_apply_controlled_not", &qerr);
            if (qerr != CL_SUCCESS || !quantum_controlled_not_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum controlled not kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_phase_oracle_kernel = clCreateKernel(quantum_program, "quantum_phase_oracle", &qerr);
            if (qerr != CL_SUCCESS || !quantum_phase_oracle_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum phase oracle kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_phase_zero_kernel = clCreateKernel(quantum_program, "quantum_phase_flip_except_zero", &qerr);
            if (qerr != CL_SUCCESS || !quantum_phase_zero_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum phase flip kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_modexp_kernel = clCreateKernel(quantum_program, "quantum_modular_exponentiation", &qerr);
            if (qerr != CL_SUCCESS || !quantum_modexp_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum modular exponentiation kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_swap_kernel = clCreateKernel(quantum_program, "quantum_swap_qubits", &qerr);
            if (qerr != CL_SUCCESS || !quantum_swap_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum swap kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_probability_kernel = clCreateKernel(quantum_program, "quantum_compute_probabilities", &qerr);
            if (qerr != CL_SUCCESS || !quantum_probability_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum probability kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_expectation_pauli_z_kernel = clCreateKernel(quantum_program, "quantum_expectation_pauli_z", &qerr);
            if (qerr != CL_SUCCESS || !quantum_expectation_pauli_z_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum expectation kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
            quantum_vqe_gradient_kernel = clCreateKernel(quantum_program, "vqe_gradient_batch_kernel", &qerr);
            if (qerr != CL_SUCCESS || !quantum_vqe_gradient_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create quantum VQE gradient kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            qualia_resonator_kernel = clCreateKernel(quantum_program, "qualia_resonator_kernel", &qerr);
            if (qerr != CL_SUCCESS || !qualia_resonator_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create qualia_resonator_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            intuition_precognition_kernel = clCreateKernel(quantum_program, "intuition_precognition_kernel", &qerr);
            if (qerr != CL_SUCCESS || !intuition_precognition_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create intuition_precognition_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            context_resonance_kernel = clCreateKernel(quantum_program, "context_resonance_kernel", &qerr);
            if (qerr != CL_SUCCESS || !context_resonance_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create context_resonance_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            dream_state_generator_kernel = clCreateKernel(quantum_program, "dream_state_generator_kernel", &qerr);
            if (qerr != CL_SUCCESS || !dream_state_generator_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create dream_state_generator_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            transformation_planner_kernel = clCreateKernel(quantum_program, "transformation_planner_kernel", &qerr);
            if (qerr != CL_SUCCESS || !transformation_planner_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create transformation_planner_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            system_narrative_kernel = clCreateKernel(quantum_program, "generate_system_narrative_kernel", &qerr);
            if (qerr != CL_SUCCESS || !system_narrative_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create generate_system_narrative_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }

            symbolic_abstraction_kernel = clCreateKernel(quantum_program, "abstract_to_symbolic_concepts_kernel", &qerr);
            if (qerr != CL_SUCCESS || !symbolic_abstraction_kernel) {
                fprintf(stderr, "[C] initialize_gpu: Failed to create abstract_to_symbolic_concepts_kernel: %s (%d)\n", clGetErrorString(qerr), qerr);
                g_quantum_enabled = 0;
                break;
            }
        } while (0);

        if (!g_quantum_enabled) {
            release_quantum_program_objects();
            release_quantum_resources();
            g_quantum_disabled_warned = 0;
            printf("[C] initialize_gpu: Quantum features disabled. Continuing without quantum kernels.\n");
        }
    }

    #undef COMPILE_KERNEL_DUAL

    if (g_quantum_enabled) {
        printf("[C] initialize_gpu: All kernels compiled successfully.\n");
    } else {
        printf("[C] initialize_gpu: All required non-quantum kernels compiled successfully.\n");
    }
    printf("[C] initialize_gpu: Initialization OK for GPU %d (%s).\n", gpu_index, deviceName);
    return 1;
}

/**
 * @brief Allocates memory on the GPU device.
 */
DLLEXPORT void *allocate_gpu_memory(int gpu_index, size_t size) {
    cl_int err;
    if (!context) {
        cc_set_last_error("[C] allocate_gpu_memory: Error - No OpenCL context available");
        fprintf(stderr, "[C] allocate_gpu_memory: Error - No OpenCL context available.\n");
        return NULL;
    }
    if (size == 0) {
        cc_set_last_error("[C] allocate_gpu_memory: Warning - Attempted to allocate 0 bytes");
        fprintf(stderr, "[C] allocate_gpu_memory: Warning - Attempted to allocate 0 bytes. Returning NULL.\n");
        return NULL;
    }
    cl_mem buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, size, NULL, &err);
    if (!buffer || err != CL_SUCCESS) {
        cc_set_last_error("[C] allocate_gpu_memory: Error - clCreateBuffer failed: %s (%d) for size %zu bytes",
                          clGetErrorString(err), err, size);
        fprintf(stderr, "[C] allocate_gpu_memory: Error - clCreateBuffer failed: %s (%d) for size %zu bytes.\n", clGetErrorString(err), err, size);
        return NULL;
    }
    return (void*)buffer;
}

/**
 * @brief Frees memory previously allocated on the GPU device.
 */
DLLEXPORT void free_gpu_memory(int gpu_index, void* buffer_handle) {
     if (!buffer_handle) { return; }
    cl_mem buffer = (cl_mem)buffer_handle;
     if (!context) { return; }
    cl_int err = clReleaseMemObject(buffer);
    if (err != CL_SUCCESS && err != CL_INVALID_MEM_OBJECT) { // Ignore errors if already freed
         fprintf(stderr, "[C] free_gpu_memory: Error - clReleaseMemObject failed for buffer %p: %s (%d)\n", buffer_handle, clGetErrorString(err), err);
    }
}

/**
 * @brief Writes data from host memory to a GPU buffer (blocking).
 */
DLLEXPORT int write_host_to_gpu_blocking(int gpu_index, void* gpu_buffer_handle, size_t offset, size_t size, const void* host_source_ptr) {
    if (!gpu_buffer_handle) {
        cc_set_last_error("[C] write_host_to_gpu_blocking: Error - Invalid GPU buffer handle (NULL)");
        fprintf(stderr, "[C] write_host_to_gpu_blocking: Error - Invalid GPU buffer handle (NULL).\n");
        return 0;
    }
    if (size > 0 && !host_source_ptr) {
        cc_set_last_error("[C] write_host_to_gpu_blocking: Error - Host source pointer is NULL but size > 0 (%zu)", size);
        fprintf(stderr, "[C] write_host_to_gpu_blocking: Error - Host source pointer is NULL but size > 0 (%zu).\n", size);
        return 0;
    }
    cl_command_queue active_queue = queue;
    if (!active_queue) {
        cc_set_last_error("[C] write_host_to_gpu_blocking: Error - Command queue is NULL");
        fprintf(stderr, "[C] write_host_to_gpu_blocking: Error - Command queue is NULL.\n");
        return 0;
    }
    g_thread_queue = active_queue;
    g_thread_gpu_index = gpu_index;
    if (size == 0) { return 1; }
    cl_mem gpu_buffer = (cl_mem)gpu_buffer_handle;
    cl_int err = clEnqueueWriteBuffer(active_queue, gpu_buffer, CL_TRUE, offset, size, host_source_ptr, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        cc_set_last_error("[C] write_host_to_gpu_blocking: Error - clEnqueueWriteBuffer failed: %s (%d) [offset=%zu, size=%zu]",
                          clGetErrorString(err), err, offset, size);
        fprintf(stderr, "[C] write_host_to_gpu_blocking: Error - clEnqueueWriteBuffer failed: %s (%d) [offset=%zu, size=%zu]\n",
                clGetErrorString(err), err, offset, size);
        return 0;
    }
    return 1;
}

/**
 * @brief Reads data from a GPU buffer to host memory (blocking).
 */
DLLEXPORT int read_gpu_to_host_blocking(int gpu_index, void* gpu_buffer_handle, size_t offset, size_t size, void* host_destination_ptr) {
    if (!gpu_buffer_handle) {
        cc_set_last_error("[C] read_gpu_to_host_blocking: Error - Invalid GPU buffer handle (NULL)");
        fprintf(stderr, "[C] read_gpu_to_host_blocking: Error - Invalid GPU buffer handle (NULL).\n");
        return 0;
    }
    if (size > 0 && !host_destination_ptr) {
        cc_set_last_error("[C] read_gpu_to_host_blocking: Error - Host destination pointer is NULL but size > 0 (%zu)", size);
        fprintf(stderr, "[C] read_gpu_to_host_blocking: Error - Host destination pointer is NULL but size > 0 (%zu).\n", size);
        return 0;
    }
    cl_command_queue active_queue = queue;
    if (!active_queue) {
        cc_set_last_error("[C] read_gpu_to_host_blocking: Error - Command queue is NULL");
        fprintf(stderr, "[C] read_gpu_to_host_blocking: Error - Command queue is NULL.\n");
        return 0;
    }
    g_thread_queue = active_queue;
    g_thread_gpu_index = gpu_index;
    if (size == 0) { return 1; }
    cl_mem gpu_buffer = (cl_mem)gpu_buffer_handle;
    cl_int err = clEnqueueReadBuffer(active_queue, gpu_buffer, CL_TRUE, offset, size, host_destination_ptr, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        cc_set_last_error("[C] read_gpu_to_host_blocking: Error - clEnqueueReadBuffer failed: %s (%d) [offset=%zu, size=%zu]",
                          clGetErrorString(err), err, offset, size);
        fprintf(stderr, "[C] read_gpu_to_host_blocking: Error - clEnqueueReadBuffer failed: %s (%d) [offset=%zu, size=%zu]\n",
                clGetErrorString(err), err, offset, size);
        return 0;
    }
    return 1;
}

static void subqg_seed_rng_state(uint64_t seed) {
    if (seed == 0) {
        seed = 0x9E3779B97F4A7C15ULL;
    }
    subqg_rng_seed = seed;
    subqg_rng_state = seed;
    if (subqg_rng_state == 0) {
        subqg_rng_state = 0x106689D45497F7ULL;
    }
}

static uint64_t subqg_next_rng64(void) {
    if (subqg_rng_state == 0) {
        subqg_seed_rng_state(subqg_rng_seed);
    }
    uint64_t x = subqg_rng_state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    subqg_rng_state = x;
    return x * 2685821657736338717ULL;
}

static float subqg_rng_next_float(void) {
    uint64_t raw = subqg_next_rng64();
    double normalized = (double)(raw >> 11) * (1.0 / 9007199254740992.0); // 2^53
    if (normalized >= 1.0) { normalized = 0.9999999999999999; }
    return (float)normalized;
}

DLLEXPORT int subqg_initialize_state(int gpu_index, float initial_energy, float initial_phase, float noise_level, float threshold) {
    return subqg_initialize_state_batched(gpu_index, 1, &initial_energy, &initial_phase, noise_level, threshold);
}

DLLEXPORT int subqg_initialize_state_batched(int gpu_index, int cell_count,
                                             const float* initial_energy, const float* initial_phase,
                                             float noise_level, float threshold) {
    (void)gpu_index;

    if (!context || !queue) {
        fprintf(stderr, "[C] subqg_initialize_state: Error - GPU context/queue not initialized. Call initialize_gpu first.\n");
        return 0;
    }
    if (!subqg_simulation_kernel) {
        fprintf(stderr, "[C] subqg_initialize_state: Error - SubQG kernel not compiled.\n");
        return 0;
    }

    if (cell_count <= 0) {
        fprintf(stderr, "[C] subqg_initialize_state_batched: Error - cell_count must be > 0 (got %d).\n", cell_count);
        return 0;
    }

    release_subqg_resources();

    subqg_width = (subqg_width > 0) ? subqg_width : cell_count;
    subqg_height = (subqg_height > 0) ? subqg_height : 1;
    subqg_field_map_elements = cell_count;

    cl_int err = CL_SUCCESS;
    const size_t fp_size = sizeof(FP_TYPE);
    const size_t int_size = sizeof(cl_int);
    size_t fp_bytes = (size_t)cell_count * fp_size;
    size_t int_bytes = (size_t)cell_count * int_size;

    subqg_energy_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);
    if (!subqg_energy_buffer || err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_initialize_state: Failed to allocate energy buffer: %s (%d)\n", clGetErrorString(err), err);
        release_subqg_resources();
        return 0;
    }

    subqg_phase_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);
    if (!subqg_phase_buffer || err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_initialize_state: Failed to allocate phase buffer: %s (%d)\n", clGetErrorString(err), err);
        release_subqg_resources();
        return 0;
    }

    subqg_interference_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);
    subqg_node_flag_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, int_bytes, NULL, &err);
    subqg_spin_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, int_bytes, NULL, &err);
    subqg_topology_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, int_bytes, NULL, &err);
    subqg_rng_energy_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);
    subqg_rng_phase_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);
    subqg_rng_spin_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);
    subqg_field_map_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, fp_bytes, NULL, &err);

    if (!subqg_interference_buffer || !subqg_node_flag_buffer || !subqg_spin_buffer || !subqg_topology_buffer ||
        !subqg_rng_energy_buffer || !subqg_rng_phase_buffer || !subqg_rng_spin_buffer || !subqg_field_map_buffer || err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_initialize_state: Failed to allocate auxiliary buffers: %s (%d)\n", clGetErrorString(err), err);
        release_subqg_resources();
        return 0;
    }

    FP_TYPE* energy_init = (FP_TYPE*)malloc(fp_bytes);
    FP_TYPE* phase_init = (FP_TYPE*)malloc(fp_bytes);
    if (!energy_init || !phase_init) {
        fprintf(stderr, "[C] subqg_initialize_state_batched: Failed to allocate host staging buffers (%zu bytes).\n", fp_bytes);
        free(energy_init);
        free(phase_init);
        release_subqg_resources();
        return 0;
    }
    for (int i = 0; i < cell_count; ++i) {
        energy_init[i] = initial_energy ? (FP_TYPE)initial_energy[i] : (FP_TYPE)0;
        phase_init[i] = initial_phase ? (FP_TYPE)initial_phase[i] : (FP_TYPE)0;
    }

    FP_TYPE zero_fp = (FP_TYPE)0;
    cl_int zero_int = 0;
    cl_int neg_one = -1;

    err = clEnqueueWriteBuffer(queue, subqg_energy_buffer, CL_TRUE, 0, fp_bytes, energy_init, 0, NULL, NULL);
    if (err == CL_SUCCESS) {
        err = clEnqueueWriteBuffer(queue, subqg_phase_buffer, CL_TRUE, 0, fp_bytes, phase_init, 0, NULL, NULL);
    }
    free(energy_init);
    free(phase_init);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_initialize_state_batched: Failed to upload initial state: %s (%d)\n", clGetErrorString(err), err);
        release_subqg_resources();
        return 0;
    }

    err = clEnqueueFillBuffer(queue, subqg_interference_buffer, &zero_fp, fp_size, 0, fp_bytes, 0, NULL, NULL);
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_node_flag_buffer, &zero_int, int_size, 0, int_bytes, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_spin_buffer, &zero_int, int_size, 0, int_bytes, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_topology_buffer, &neg_one, int_size, 0, int_bytes, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_rng_energy_buffer, &zero_fp, fp_size, 0, fp_bytes, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_rng_phase_buffer, &zero_fp, fp_size, 0, fp_bytes, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_rng_spin_buffer, &zero_fp, fp_size, 0, fp_bytes, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueFillBuffer(queue, subqg_field_map_buffer, &zero_fp, fp_size, 0, fp_bytes, 0, NULL, NULL);
    }
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_initialize_state_batched: Failed to initialize buffers: %s (%d)\n", clGetErrorString(err), err);
        release_subqg_resources();
        return 0;
    }

    subqg_noise_level = noise_level;
    subqg_threshold = threshold;
    subqg_cell_count = cell_count;
    subqg_state_initialized = 1;
    if (subqg_deterministic_mode) {
        subqg_seed_rng_state(subqg_rng_seed);
    }

    return 1;
}

DLLEXPORT int subqg_debug_read_field(float* out_host, int max_len) {
    if (!out_host || max_len <= 0) {
        return 0;
    }
    if (!queue || !subqg_field_map_buffer || subqg_cell_count <= 0) {
        return 0;
    }

    size_t elems = (size_t)subqg_cell_count;
    size_t nread = (size_t)((max_len < (int)elems) ? max_len : (int)elems);
    if (nread == 0) {
        return 0;
    }

    size_t nbytes = nread * sizeof(float);
    cl_int err = clEnqueueReadBuffer(queue, subqg_field_map_buffer, CL_TRUE,
                                     0, nbytes, out_host, 0, NULL, NULL);
    return (err == CL_SUCCESS) ? (int)nread : 0;
}

DLLEXPORT int subqg_simulation_step(int gpu_index, float rng_energy, float rng_phase, float rng_spin,
                                    float* out_energy, float* out_phase, float* out_interference,
                                    int* out_node_flag, int* out_spin, int* out_topology,
                                    float* out_field_map, int field_map_length) {
    float energy_rng = rng_energy;
    float phase_rng = rng_phase;
    float spin_rng = rng_spin;
    float energy_tmp = 0.0f;
    float phase_tmp = 0.0f;
    float interference_tmp = 0.0f;
    int node_tmp = 0;
    int spin_tmp = 0;
    int topo_tmp = -1;

    int cells = (subqg_cell_count > 0) ? subqg_cell_count : 1;

    float* field_map_tmp = NULL;
    if (out_field_map && field_map_length > 0) {
        field_map_tmp = (float*)malloc(sizeof(float) * (size_t)field_map_length);
        if (!field_map_tmp) {
            fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate field_map staging buffer (%zu bytes).\n",
                    sizeof(float) * (size_t)field_map_length);
            return 0;
        }
    }

    float* rng_energy_array = NULL;
    float* rng_phase_array = NULL;
    float* rng_spin_array = NULL;
    int free_rng_arrays = 0;
    if (cells > 1) {
        size_t fp_bytes = sizeof(float) * (size_t)cells;
        rng_energy_array = (float*)malloc(fp_bytes);
        rng_phase_array = (float*)malloc(fp_bytes);
        rng_spin_array = (float*)malloc(fp_bytes);
        if (!rng_energy_array || !rng_phase_array || !rng_spin_array) {
            fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate RNG arrays (%zu bytes).\n", fp_bytes);
            free(rng_energy_array);
            free(rng_phase_array);
            free(rng_spin_array);
            free(field_map_tmp);
            return 0;
        }
        for (int i = 0; i < cells; ++i) {
            rng_energy_array[i] = energy_rng;
            rng_phase_array[i] = phase_rng;
            rng_spin_array[i] = spin_rng;
        }
        free_rng_arrays = 1;
    } else {
        rng_energy_array = &energy_rng;
        rng_phase_array = &phase_rng;
        rng_spin_array = &spin_rng;
    }

    float* energy_array = NULL;
    float* phase_array = NULL;
    float* interference_array = NULL;
    int* node_array = NULL;
    int* spin_array = NULL;
    int* topo_array = NULL;
    int free_output_arrays = (cells > 1);
    int ok = 0;

    if (cells > 1) {
        if (out_energy) {
            energy_array = (float*)malloc(sizeof(float) * (size_t)cells);
            if (!energy_array) { fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate energy array.\n"); goto cleanup_fail; }
        }
        if (out_phase) {
            phase_array = (float*)malloc(sizeof(float) * (size_t)cells);
            if (!phase_array) { fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate phase array.\n"); goto cleanup_fail; }
        }
        if (out_interference) {
            interference_array = (float*)malloc(sizeof(float) * (size_t)cells);
            if (!interference_array) { fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate interference array.\n"); goto cleanup_fail; }
        }
        if (out_node_flag) {
            node_array = (int*)malloc(sizeof(int) * (size_t)cells);
            if (!node_array) { fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate node array.\n"); goto cleanup_fail; }
        }
        if (out_spin) {
            spin_array = (int*)malloc(sizeof(int) * (size_t)cells);
            if (!spin_array) { fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate spin array.\n"); goto cleanup_fail; }
        }
        if (out_topology) {
            topo_array = (int*)malloc(sizeof(int) * (size_t)cells);
            if (!topo_array) { fprintf(stderr, "[C] subqg_simulation_step: Failed to allocate topology array.\n"); goto cleanup_fail; }
        }
    } else {
        energy_array = out_energy ? &energy_tmp : NULL;
        phase_array = out_phase ? &phase_tmp : NULL;
        interference_array = out_interference ? &interference_tmp : NULL;
        node_array = out_node_flag ? &node_tmp : NULL;
        spin_array = out_spin ? &spin_tmp : NULL;
        topo_array = out_topology ? &topo_tmp : NULL;
    }

    ok = subqg_simulation_step_batched(gpu_index,
                                       rng_energy_array, rng_phase_array, rng_spin_array,
                                       cells,
                                       energy_array,
                                       phase_array,
                                       interference_array,
                                       node_array,
                                       spin_array,
                                       topo_array,
                                       field_map_tmp, field_map_length);

    if (ok) {
        if (out_energy) {
            if (cells == 1) {
                *out_energy = energy_tmp;
            } else {
                double accum = 0.0;
                for (int i = 0; i < cells; ++i) { accum += energy_array[i]; }
                *out_energy = (float)(accum / (double)cells);
            }
        }
        if (out_phase) {
            if (cells == 1) {
                *out_phase = phase_tmp;
            } else {
                double accum = 0.0;
                for (int i = 0; i < cells; ++i) { accum += phase_array[i]; }
                *out_phase = (float)(accum / (double)cells);
            }
        }
        if (out_interference) {
            if (cells == 1) {
                *out_interference = interference_tmp;
            } else {
                double accum = 0.0;
                for (int i = 0; i < cells; ++i) { accum += interference_array[i]; }
                *out_interference = (float)(accum / (double)cells);
            }
        }
        if (out_node_flag) {
            *out_node_flag = (cells == 1) ? node_tmp : node_array[0];
        }
        if (out_spin) {
            *out_spin = (cells == 1) ? spin_tmp : spin_array[0];
        }
        if (out_topology) {
            *out_topology = (cells == 1) ? topo_tmp : topo_array[0];
        }
        if (out_field_map && field_map_tmp) {
            memcpy(out_field_map, field_map_tmp, sizeof(float) * (size_t)field_map_length);
        }
    }

    if (free_output_arrays) {
        free(energy_array);
        free(phase_array);
        free(interference_array);
        free(node_array);
        free(spin_array);
        free(topo_array);
    }

    if (free_rng_arrays) {
        free(rng_energy_array);
        free(rng_phase_array);
        free(rng_spin_array);
    }

    if (field_map_tmp) {
        free(field_map_tmp);
    }

    return ok;

cleanup_fail:
    if (energy_array && free_output_arrays) { free(energy_array); }
    if (phase_array && free_output_arrays) { free(phase_array); }
    if (interference_array && free_output_arrays) { free(interference_array); }
    if (node_array && free_output_arrays) { free(node_array); }
    if (spin_array && free_output_arrays) { free(spin_array); }
    if (topo_array && free_output_arrays) { free(topo_array); }
    if (free_rng_arrays) {
        free(rng_energy_array);
        free(rng_phase_array);
        free(rng_spin_array);
    }
    if (field_map_tmp) {
        free(field_map_tmp);
    }
    return 0;
}

DLLEXPORT int subqg_simulation_step_batched(int gpu_index,
                                            const float* rng_energy, const float* rng_phase, const float* rng_spin,
                                            int batch_count,
                                            float* out_energy, float* out_phase, float* out_interference,
                                            int* out_node_flag, int* out_spin, int* out_topology,
                                            float* out_field_map, int field_map_length) {
    (void)gpu_index;
    if (!subqg_state_initialized) {
        int gw = (subqg_width > 0) ? subqg_width : subqg_cell_count;
        int gh = (subqg_height > 0) ? subqg_height : 1;
        if (gw <= 0) {
            gw = (batch_count > 0) ? batch_count : subqg_cell_count;
        }
        if (gh <= 0) {
            gh = 1;
        }
        if (!ensure_subqg_state(gw, gh)) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Error - State not initialized and auto-init failed.\n");
            return 0;
        }
    }
    if (!queue || !subqg_simulation_kernel) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Error - Missing queue or kernel.\n");
        return 0;
    }
    if (subqg_cell_count <= 0) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Internal error - invalid cell count %d.\n", subqg_cell_count);
        return 0;
    }

    int cells = subqg_cell_count;
    if (batch_count == 0) {
        batch_count = cells;
    }
    if (batch_count != cells) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: batch_count (%d) must match initialized cell count (%d).\n", batch_count, cells);
        return 0;
    }

    if (out_field_map && field_map_length < subqg_field_map_elements) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: field_map_length (%d) smaller than required elements (%d).\n",
                field_map_length, subqg_field_map_elements);
        return 0;
    }

    size_t fp_bytes = (size_t)cells * sizeof(FP_TYPE);
    FP_TYPE* rng_energy_fp = (FP_TYPE*)malloc(fp_bytes);
    FP_TYPE* rng_phase_fp = (FP_TYPE*)malloc(fp_bytes);
    FP_TYPE* rng_spin_fp = (FP_TYPE*)malloc(fp_bytes);
    if (!rng_energy_fp || !rng_phase_fp || !rng_spin_fp) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate RNG staging buffers (%zu bytes).\n", fp_bytes);
        free(rng_energy_fp);
        free(rng_phase_fp);
        free(rng_spin_fp);
        return 0;
    }

    int use_external_rng = (rng_energy && rng_phase && rng_spin);
    if (!use_external_rng && subqg_deterministic_mode) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Deterministic mode requires explicit RNG arrays.\n");
        free(rng_energy_fp);
        free(rng_phase_fp);
        free(rng_spin_fp);
        return 0;
    }

    for (int i = 0; i < cells; ++i) {
        if (use_external_rng) {
            rng_energy_fp[i] = (FP_TYPE)rng_energy[i];
            rng_phase_fp[i] = (FP_TYPE)rng_phase[i];
            rng_spin_fp[i] = (FP_TYPE)rng_spin[i];
        } else {
            rng_energy_fp[i] = (FP_TYPE)subqg_rng_next_float();
            rng_phase_fp[i] = (FP_TYPE)subqg_rng_next_float();
            rng_spin_fp[i] = (FP_TYPE)subqg_rng_next_float();
        }
    }

    cl_int err = clEnqueueWriteBuffer(queue, subqg_rng_energy_buffer, CL_TRUE, 0, fp_bytes, rng_energy_fp, 0, NULL, NULL);
    if (err == CL_SUCCESS) {
        err = clEnqueueWriteBuffer(queue, subqg_rng_phase_buffer, CL_TRUE, 0, fp_bytes, rng_phase_fp, 0, NULL, NULL);
    }
    if (err == CL_SUCCESS) {
        err = clEnqueueWriteBuffer(queue, subqg_rng_spin_buffer, CL_TRUE, 0, fp_bytes, rng_spin_fp, 0, NULL, NULL);
    }

    free(rng_energy_fp);
    free(rng_phase_fp);
    free(rng_spin_fp);

    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to upload RNG buffers: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    FP_TYPE noise_level_fp = (FP_TYPE)subqg_noise_level;
    FP_TYPE threshold_fp = (FP_TYPE)subqg_threshold;
    FP_TYPE noise_factor_fp = (FP_TYPE)get_noise_factor();
    cl_int cell_count_cl = (cl_int)cells;
    cl_int write_field_map = 1;

    int arg_index = 0;
    err = CL_SUCCESS;
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_energy_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_phase_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_interference_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_node_flag_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_spin_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_topology_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_rng_energy_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_rng_phase_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_rng_spin_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(FP_TYPE), &noise_level_fp);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(FP_TYPE), &threshold_fp);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(FP_TYPE), &noise_factor_fp);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_int), &cell_count_cl);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_mem), &subqg_field_map_buffer);
    err |= clSetKernelArg(subqg_simulation_kernel, arg_index++, sizeof(cl_int), &write_field_map);

    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    size_t global_work_size = (size_t)cells;
    err = ENQUEUE_KERNEL_PROFILED(subqg_simulation_kernel, 1, &global_work_size, NULL, "subqg_simulation_step");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to enqueue kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_simulation_step_batched: clFinish failed: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    if (out_energy) {
        FP_TYPE* energy_host = (FP_TYPE*)malloc(fp_bytes);
        if (!energy_host) { fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate energy host buffer (%zu bytes).\n", fp_bytes); return 0; }
        err = clEnqueueReadBuffer(queue, subqg_energy_buffer, CL_TRUE, 0, fp_bytes, energy_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read energy buffer: %s (%d)\n", clGetErrorString(err), err);
            free(energy_host);
            return 0;
        }
        for (int i = 0; i < cells; ++i) { out_energy[i] = (float)energy_host[i]; }
        free(energy_host);
    }
    if (out_phase) {
        FP_TYPE* phase_host = (FP_TYPE*)malloc(fp_bytes);
        if (!phase_host) { fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate phase host buffer (%zu bytes).\n", fp_bytes); return 0; }
        err = clEnqueueReadBuffer(queue, subqg_phase_buffer, CL_TRUE, 0, fp_bytes, phase_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read phase buffer: %s (%d)\n", clGetErrorString(err), err);
            free(phase_host);
            return 0;
        }
        for (int i = 0; i < cells; ++i) { out_phase[i] = (float)phase_host[i]; }
        free(phase_host);
    }
    if (out_interference) {
        FP_TYPE* interference_host = (FP_TYPE*)malloc(fp_bytes);
        if (!interference_host) { fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate interference host buffer (%zu bytes).\n", fp_bytes); return 0; }
        err = clEnqueueReadBuffer(queue, subqg_interference_buffer, CL_TRUE, 0, fp_bytes, interference_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read interference buffer: %s (%d)\n", clGetErrorString(err), err);
            free(interference_host);
            return 0;
        }
        for (int i = 0; i < cells; ++i) { out_interference[i] = (float)interference_host[i]; }
        free(interference_host);
    }

    size_t int_bytes = (size_t)cells * sizeof(cl_int);
    if (out_node_flag) {
        cl_int* node_host = (cl_int*)malloc(int_bytes);
        if (!node_host) { fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate node flag host buffer (%zu bytes).\n", int_bytes); return 0; }
        err = clEnqueueReadBuffer(queue, subqg_node_flag_buffer, CL_TRUE, 0, int_bytes, node_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read node flag buffer: %s (%d)\n", clGetErrorString(err), err);
            free(node_host);
            return 0;
        }
        for (int i = 0; i < cells; ++i) { out_node_flag[i] = (int)node_host[i]; }
        free(node_host);
    }
    if (out_spin) {
        cl_int* spin_host = (cl_int*)malloc(int_bytes);
        if (!spin_host) { fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate spin host buffer (%zu bytes).\n", int_bytes); return 0; }
        err = clEnqueueReadBuffer(queue, subqg_spin_buffer, CL_TRUE, 0, int_bytes, spin_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read spin buffer: %s (%d)\n", clGetErrorString(err), err);
            free(spin_host);
            return 0;
        }
        for (int i = 0; i < cells; ++i) { out_spin[i] = (int)spin_host[i]; }
        free(spin_host);
    }
    if (out_topology) {
        cl_int* topo_host = (cl_int*)malloc(int_bytes);
        if (!topo_host) { fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate topology host buffer (%zu bytes).\n", int_bytes); return 0; }
        err = clEnqueueReadBuffer(queue, subqg_topology_buffer, CL_TRUE, 0, int_bytes, topo_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read topology buffer: %s (%d)\n", clGetErrorString(err), err);
            free(topo_host);
            return 0;
        }
        for (int i = 0; i < cells; ++i) { out_topology[i] = (int)topo_host[i]; }
        free(topo_host);
    }
    if (out_field_map) {
        size_t map_bytes = (size_t)subqg_field_map_elements * sizeof(FP_TYPE);
        FP_TYPE* field_map_host = (FP_TYPE*)malloc(map_bytes);
        if (!field_map_host) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to allocate field_map host buffer (%zu bytes).\n", map_bytes);
            return 0;
        }
        err = clEnqueueReadBuffer(queue, subqg_field_map_buffer, CL_TRUE, 0, map_bytes, field_map_host, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_simulation_step_batched: Failed to read field_map buffer: %s (%d)\n", clGetErrorString(err), err);
            free(field_map_host);
            return 0;
        }
        size_t copy_elems = (size_t)field_map_length;
        if (copy_elems > (size_t)subqg_field_map_elements) {
            copy_elems = (size_t)subqg_field_map_elements;
        }
        for (size_t i = 0; i < copy_elems; ++i) {
            out_field_map[i] = (float)field_map_host[i];
        }
        free(field_map_host);
    }

    return 1;
}

DLLEXPORT int subqg_inject_agents(int gpu_index, const HPIOAgent* agents, int count) {
    (void)gpu_index;
    int gw = (subqg_width > 0) ? subqg_width : subqg_cell_count;
    int gh = (subqg_height > 0) ? subqg_height : 1;
    if (!subqg_state_initialized) {
        if (!ensure_subqg_state(gw, gh)) {
            fprintf(stderr, "[C] subqg_inject_agents: Error - State not initialized and auto-init failed.\n");
            return 0;
        }
    }
    if (!subqg_agent_kernel) {
        fprintf(stderr, "[C] subqg_inject_agents: Error - Agent kernel not compiled.\n");
        return 0;
    }
    if (count <= 0 || !agents) {
        return 1;
    }
    size_t required_bytes = (size_t)count * sizeof(HPIOAgent);
    if (!subqg_agent_buffer || subqg_agent_buffer_bytes < required_bytes) {
        if (subqg_agent_buffer) {
            clReleaseMemObject(subqg_agent_buffer);
            subqg_agent_buffer = NULL;
            subqg_agent_buffer_bytes = 0;
        }
        cl_int err = CL_SUCCESS;
        subqg_agent_buffer = clCreateBuffer(context, CL_MEM_READ_ONLY, required_bytes, NULL, &err);
        if (!subqg_agent_buffer || err != CL_SUCCESS) {
            fprintf(stderr, "[C] subqg_inject_agents: Failed to allocate agent buffer: %s (%d)\n", clGetErrorString(err), err);
            return 0;
        }
        subqg_agent_buffer_bytes = required_bytes;
    }

    cl_int err = clEnqueueWriteBuffer(queue, subqg_agent_buffer, CL_TRUE, 0, required_bytes, agents, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_inject_agents: Failed to upload agents: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    cl_int agent_count_cl = (cl_int)count;
    cl_int grid_w = (cl_int)(subqg_width > 0 ? subqg_width : subqg_cell_count);
    cl_int grid_h = (cl_int)(subqg_height > 0 ? subqg_height : 1);

    int arg = 0;
    err  = clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_mem), &subqg_energy_buffer);
    err |= clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_mem), &subqg_phase_buffer);
    err |= clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_mem), &subqg_field_map_buffer);
    err |= clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_mem), &subqg_agent_buffer);
    err |= clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_int), &agent_count_cl);
    err |= clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_int), &grid_w);
    err |= clSetKernelArg(subqg_agent_kernel, arg++, sizeof(cl_int), &grid_h);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_inject_agents: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    size_t global = (size_t)(subqg_field_map_elements > 0 ? subqg_field_map_elements : subqg_cell_count);
    err = ENQUEUE_KERNEL_PROFILED(subqg_agent_kernel, 1, &global, NULL, "subqg_inject_agents");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] subqg_inject_agents: Kernel launch failed: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

// ---------------------------------------------------------------------------
// Mycel / pheromone hybrid state management (host-side reference implementation)
// ---------------------------------------------------------------------------

DLLEXPORT int subqg_init_mycel(int gpu_index, int T_cap, int C, int K) {
    (void)gpu_index;
    if (T_cap <= 0 || C <= 0 || K <= 0) {
        cc_set_last_error("subqg_init_mycel: invalid params (T=%d C=%d K=%d)", T_cap, C, K);
        return 0;
    }
    if (!context || !queue) {
        cc_set_last_error("subqg_init_mycel: OpenCL not initialized");
        return 0;
    }
    if (!mycel_initialize(&g_mycel_state, T_cap, C, K)) {
        cc_set_last_error("subqg_init_mycel: mycel_initialize() failed");
        return 0;
    }
    /* Standard-Defaults – kann man später von außen setzen */
    g_mycel_state.decay_default = 0.01f;
    g_mycel_state.diffu_default = 0.10f;
    g_mycel_state.T_act = (T_cap < 128 ? T_cap : 128);

    /* Initiale Koloniebelegung: markiere die ersten aktiven Plätze als lebendig */
    int active = g_mycel_state.T_act;
    if (active < 0) { active = 0; }
    if (active > g_mycel_state.T_cap) { active = g_mycel_state.T_cap; }

    if (g_mycel_state.alive) {
        for (int t = 0; t < g_mycel_state.T_cap; ++t) {
            g_mycel_state.alive[t] = (t < active) ? 1 : 0;
        }
    }
    if (g_mycel_state.colony_id) {
        for (int t = 0; t < active; ++t) {
            g_mycel_state.colony_id[t] = (uint8_t)((t % 255) + 1);
        }
        for (int t = active; t < g_mycel_state.T_cap; ++t) {
            g_mycel_state.colony_id[t] = 0;
        }
    }
    if (g_mycel_state.nutrient) {
        for (int t = 0; t < g_mycel_state.T_cap; ++t) {
            g_mycel_state.nutrient[t] = (t < active) ? 1.0f : 0.0f;
        }
    }
    if (g_mycel_state.mood) {
        for (int t = 0; t < g_mycel_state.T_cap; ++t) {
            for (int c = 0; c < g_mycel_state.C; ++c) {
                float phase = (float)c / (float)(g_mycel_state.C > 0 ? g_mycel_state.C : 1);
                g_mycel_state.mood[t * g_mycel_state.C + c] = (t < active) ? phase : 0.0f;
            }
        }
    }
    if (g_mycel_state.free_list) {
        int free_slots = g_mycel_state.T_cap - active;
        if (free_slots < 0) { free_slots = 0; }
        for (int i = 0; i < free_slots; ++i) {
            g_mycel_state.free_list[i] = active + i;
        }
        g_mycel_state.free_head = free_slots;
    }

    if (!mycel_upload_all_state(&g_mycel_state)) {
        cc_set_last_error("subqg_init_mycel: failed to upload seeded state");
        return 0;
    }

    fprintf(stderr,
            "[C] subqg_init_mycel: Seeded %d active tiles (C=%d, K=%d) with default nutrient field.\n",
            active, g_mycel_state.C, g_mycel_state.K);

    return 1;
}

DLLEXPORT int subqg_set_active_T(int gpu_index, int T_act) {
    (void)gpu_index;
    if (!g_mycel_state.initialized) {
        cc_set_last_error("subqg_set_active_T: state not initialized");
        return 0;
    }
    if (T_act < 0) T_act = 0;
    if (T_act > g_mycel_state.T_cap) T_act = g_mycel_state.T_cap;
    g_mycel_state.T_act = T_act;
    return 1;
}

DLLEXPORT int subqg_realloc_pheromone_channels(int gpu_index, int new_C) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] subqg_realloc_pheromone_channels: State not initialized.\n");
        return 0;
    }
    if (new_C <= 0) {
        fprintf(stderr, "[C] subqg_realloc_pheromone_channels: Invalid channel count %d.\n", new_C);
        return 0;
    }
    if (new_C == st->C) {
        return 1;
    }
    int old_C = st->C;
    size_t edge_count = mycel_edge_count(st);
    mycel_release_gpu_buffers(st);
    size_t new_pher_count = (size_t)new_C * edge_count;
    size_t new_mood_count = (size_t)new_C * (size_t)st->T_cap;
    float* new_pheromone = (float*)calloc(new_pher_count, sizeof(float));
    float* new_mood = (float*)calloc(new_mood_count, sizeof(float));
    float* new_reinforce = (float*)calloc((size_t)new_C, sizeof(float));
    float* new_kappa = (float*)calloc((size_t)new_C, sizeof(float));
    if (!new_pheromone || !new_mood || !new_reinforce || !new_kappa) {
        fprintf(stderr, "[C] subqg_realloc_pheromone_channels: Allocation failed for C=%d.\n", new_C);
        free(new_pheromone);
        free(new_mood);
        free(new_reinforce);
        free(new_kappa);
        return 0;
    }
    int copy_C = (new_C < old_C) ? new_C : old_C;
    for (size_t edge = 0; edge < edge_count; ++edge) {
        float* dst = new_pheromone + edge * (size_t)new_C;
        float* src = st->pheromone + edge * (size_t)old_C;
        if (copy_C > 0) {
            memcpy(dst, src, (size_t)copy_C * sizeof(float));
        }
    }
    for (int t = 0; t < st->T_cap; ++t) {
        float* dst = new_mood + (size_t)t * (size_t)new_C;
        float* src = st->mood + (size_t)t * (size_t)old_C;
        if (copy_C > 0) {
            memcpy(dst, src, (size_t)copy_C * sizeof(float));
        }
    }
    if (copy_C > 0) {
        memcpy(new_reinforce, st->reinforce_gain, (size_t)copy_C * sizeof(float));
        memcpy(new_kappa, st->kappa_mood, (size_t)copy_C * sizeof(float));
    }
    free(st->pheromone);
    free(st->mood);
    free(st->reinforce_gain);
    free(st->kappa_mood);
    st->pheromone = new_pheromone;
    st->mood = new_mood;
    st->reinforce_gain = new_reinforce;
    st->kappa_mood = new_kappa;
    st->C = new_C;
    if (!mycel_upload_all_state(st)) {
        return 0;
    }
    return 1;
}

DLLEXPORT int subqg_set_repro_params(int gpu_index, float thr_nu, float thr_act, float mut_sigma) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] subqg_set_repro_params: State not initialized.\n");
        return 0;
    }
    st->repro_thr_nutrient = thr_nu;
    st->repro_thr_activity = thr_act;
    st->repro_mut_sigma = mut_sigma;
    return 1;
}

DLLEXPORT int subqg_set_nutrient_recovery(int gpu_index, float recovery_rate) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] subqg_set_nutrient_recovery: State not initialized.\n");
        return 0;
    }
    if (recovery_rate < 0.0f) {
        recovery_rate = 0.0f;
    }
    st->nutrient_recovery = recovery_rate;
    return 1;
}

DLLEXPORT int set_pheromone_gains(int gpu_index, const float* gain_C, int count) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] set_pheromone_gains: State not initialized.\n");
        return 0;
    }
    if (!gain_C || count <= 0) {
        fprintf(stderr, "[C] set_pheromone_gains: Invalid gain array.\n");
        return 0;
    }
    if (!mycel_ensure_gpu_buffers(st)) {
        return 0;
    }
    int copy = (count < st->C) ? count : st->C;
    memcpy(st->reinforce_gain, gain_C, (size_t)copy * sizeof(float));
    for (int i = copy; i < st->C; ++i) {
        st->reinforce_gain[i] = 0.0f;
    }
    if (!mycel_upload_buffer(st->reinforce_gain_buf, st->reinforce_gain, (size_t)st->C * sizeof(float), "reinforce_gain")) {
        return 0;
    }
    return 1;
}

DLLEXPORT int set_diffusion_params(int gpu_index, float decay_default, float diffu_default) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] set_diffusion_params: State not initialized.\n");
        return 0;
    }
    if (!mycel_ensure_gpu_buffers(st)) {
        return 0;
    }
    st->decay_default = decay_default;
    st->diffu_default = diffu_default;
    size_t edge_count = mycel_edge_count(st);
    for (size_t i = 0; i < edge_count; ++i) {
        st->decay[i] = decay_default;
        st->diffu[i] = diffu_default;
    }
    if (!mycel_upload_buffer(st->decay_buf, st->decay, edge_count * sizeof(float), "decay") ||
        !mycel_upload_buffer(st->diffu_buf, st->diffu, edge_count * sizeof(float), "diffu")) {
        return 0;
    }
    return 1;
}

DLLEXPORT int set_neighbors_sparse(int gpu_index, const int* neigh_idx_TK) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] set_neighbors_sparse: State not initialized.\n");
        return 0;
    }
    if (!neigh_idx_TK) {
        fprintf(stderr, "[C] set_neighbors_sparse: neigh_idx pointer is NULL.\n");
        return 0;
    }
    if (!mycel_ensure_gpu_buffers(st)) {
        return 0;
    }
    size_t total = mycel_edge_count(st);
    memcpy(st->neigh_idx, neigh_idx_TK, total * sizeof(int));
    if (!mycel_upload_buffer(st->neigh_idx_buf, st->neigh_idx, total * sizeof(int), "neigh_idx")) {
        return 0;
    }
    return 1;
}

DLLEXPORT int set_mood_state(int gpu_index, const float* mood_tC) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] set_mood_state: State not initialized.\n");
        return 0;
    }
    if (!mood_tC) {
        fprintf(stderr, "[C] set_mood_state: mood array is NULL.\n");
        return 0;
    }
    if (!mycel_ensure_gpu_buffers(st)) {
        return 0;
    }
    size_t count = (size_t)st->T_cap * (size_t)st->C;
    memcpy(st->mood, mood_tC, count * sizeof(float));
    if (!mycel_upload_buffer(st->mood_buf, st->mood, count * sizeof(float), "mood")) {
        return 0;
    }
    return 1;
}

DLLEXPORT int set_nutrient_state(int gpu_index, const float* nutrient_t) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] set_nutrient_state: State not initialized.\n");
        return 0;
    }
    if (!nutrient_t) {
        fprintf(stderr, "[C] set_nutrient_state: nutrient array is NULL.\n");
        return 0;
    }
    if (!mycel_ensure_gpu_buffers(st)) {
        return 0;
    }
    memcpy(st->nutrient, nutrient_t, (size_t)st->T_cap * sizeof(float));
    if (!mycel_upload_buffer(st->nutrient_buf, st->nutrient, (size_t)st->T_cap * sizeof(float), "nutrient")) {
        return 0;
    }
    return 1;
}

DLLEXPORT int step_pheromone_reinforce(int gpu_index, const float* activity_t) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_pheromone_reinforce: State not initialized.\n");
        return 0;
    }
    if (!activity_t) {
        fprintf(stderr, "[C] step_pheromone_reinforce: activity pointer is NULL.\n");
        return 0;
    }
    if (!mycel_upload_all_state(st)) {
        return 0;
    }
    size_t edge_count = mycel_edge_count(st);
    size_t pher_bytes = edge_count * (size_t)st->C * sizeof(float);
    size_t activity_bytes = (size_t)st->T_cap * sizeof(float);
    cl_int err = CL_SUCCESS;
    cl_mem activity_buf = NULL;
    if (activity_bytes > 0) {
        activity_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, activity_bytes, NULL, &err);
        if (!activity_buf || err != CL_SUCCESS) {
            fprintf(stderr, "[C] step_pheromone_reinforce: Failed to allocate activity buffer: %s (%d).\n", clGetErrorString(err), err);
            if (activity_buf) { clReleaseMemObject(activity_buf); }
            return 0;
        }
        err = clEnqueueWriteBuffer(queue, activity_buf, CL_TRUE, 0, activity_bytes, activity_t, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] step_pheromone_reinforce: Failed to upload activity: %s (%d).\n", clGetErrorString(err), err);
            clReleaseMemObject(activity_buf);
            return 0;
        }
    }

    cl_int T_act = (cl_int)st->T_act;
    cl_int T_cap = (cl_int)st->T_cap;
    cl_int K = (cl_int)st->K;
    cl_int C = (cl_int)st->C;
    int arg = 0;
    err = CL_SUCCESS;
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_mem), &st->pheromone_buf);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_mem), &st->neigh_idx_buf);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_mem), &st->alive_buf);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_mem), &st->mood_buf);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_mem), &st->reinforce_gain_buf);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_mem), &activity_buf);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_int), &T_act);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_int), &T_cap);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_int), &K);
    err |= clSetKernelArg(mycel_reinforce_kernel, arg++, sizeof(cl_int), &C);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_pheromone_reinforce: Failed to set kernel args: %s (%d).\n", clGetErrorString(err), err);
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }

    size_t global = 1;
    err = clEnqueueNDRangeKernel(queue, mycel_reinforce_kernel, 1, NULL, &global, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_pheromone_reinforce: Failed to enqueue kernel: %s (%d).\n", clGetErrorString(err), err);
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_pheromone_reinforce: clFinish failed: %s (%d).\n", clGetErrorString(err), err);
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    if (!mycel_download_buffer(st->pheromone_buf, st->pheromone, pher_bytes, "pheromone")) {
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    if (activity_buf) {
        clReleaseMemObject(activity_buf);
    }
    return 1;
}

DLLEXPORT int step_pheromone_diffuse_decay(int gpu_index) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_pheromone_diffuse_decay: State not initialized.\n");
        return 0;
    }
    if (!mycel_upload_all_state(st)) {
        return 0;
    }
    size_t edge_count = mycel_edge_count(st);
    size_t pher_bytes = edge_count * (size_t)st->C * sizeof(float);
    cl_int err = CL_SUCCESS;
    cl_int T_act = (cl_int)st->T_act;
    cl_int T_cap = (cl_int)st->T_cap;
    cl_int K = (cl_int)st->K;
    cl_int C = (cl_int)st->C;
    int arg = 0;
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_mem), &st->pheromone_buf);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_mem), &st->neigh_idx_buf);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_mem), &st->alive_buf);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_mem), &st->decay_buf);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_mem), &st->diffu_buf);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_int), &T_act);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_int), &T_cap);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_int), &K);
    err |= clSetKernelArg(mycel_diffuse_kernel, arg++, sizeof(cl_int), &C);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_pheromone_diffuse_decay: Failed to set kernel args: %s (%d).\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = 1;
    err = clEnqueueNDRangeKernel(queue, mycel_diffuse_kernel, 1, NULL, &global, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_pheromone_diffuse_decay: Failed to enqueue kernel: %s (%d).\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_pheromone_diffuse_decay: clFinish failed: %s (%d).\n", clGetErrorString(err), err);
        return 0;
    }
    if (!mycel_download_buffer(st->pheromone_buf, st->pheromone, pher_bytes, "pheromone")) {
        return 0;
    }
    return 1;
}

DLLEXPORT int step_mycel_update(int gpu_index, const float* activity_t) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_mycel_update: State not initialized.\n");
        return 0;
    }
    if (!activity_t) {
        fprintf(stderr, "[C] step_mycel_update: activity pointer is NULL.\n");
        return 0;
    }
    if (!mycel_upload_all_state(st)) {
        return 0;
    }
    size_t nutrient_bytes = (size_t)st->T_cap * sizeof(float);
    size_t activity_bytes = (size_t)st->T_cap * sizeof(float);
    cl_int err = CL_SUCCESS;
    cl_mem activity_buf = NULL;
    if (activity_bytes > 0) {
        activity_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, activity_bytes, NULL, &err);
        if (!activity_buf || err != CL_SUCCESS) {
            fprintf(stderr, "[C] step_mycel_update: Failed to allocate activity buffer: %s (%d).\n", clGetErrorString(err), err);
            if (activity_buf) { clReleaseMemObject(activity_buf); }
            return 0;
        }
        err = clEnqueueWriteBuffer(queue, activity_buf, CL_TRUE, 0, activity_bytes, activity_t, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] step_mycel_update: Failed to upload activity: %s (%d).\n", clGetErrorString(err), err);
            clReleaseMemObject(activity_buf);
            return 0;
        }
    }

    cl_int T_act = (cl_int)st->T_act;
    cl_float recovery = (cl_float)st->nutrient_recovery;
    int arg = 0;
    err = CL_SUCCESS;
    err |= clSetKernelArg(mycel_nutrient_kernel, arg++, sizeof(cl_mem), &st->nutrient_buf);
    err |= clSetKernelArg(mycel_nutrient_kernel, arg++, sizeof(cl_mem), &st->alive_buf);
    err |= clSetKernelArg(mycel_nutrient_kernel, arg++, sizeof(cl_mem), &activity_buf);
    err |= clSetKernelArg(mycel_nutrient_kernel, arg++, sizeof(cl_float), &recovery);
    err |= clSetKernelArg(mycel_nutrient_kernel, arg++, sizeof(cl_int), &T_act);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_mycel_update: Failed to set kernel args: %s (%d).\n", clGetErrorString(err), err);
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    size_t global = 1;
    err = clEnqueueNDRangeKernel(queue, mycel_nutrient_kernel, 1, NULL, &global, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_mycel_update: Failed to enqueue kernel: %s (%d).\n", clGetErrorString(err), err);
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] step_mycel_update: clFinish failed: %s (%d).\n", clGetErrorString(err), err);
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    if (!mycel_download_buffer(st->nutrient_buf, st->nutrient, nutrient_bytes, "nutrient")) {
        if (activity_buf) { clReleaseMemObject(activity_buf); }
        return 0;
    }
    if (activity_buf) {
        clReleaseMemObject(activity_buf);
    }
    return 1;
}

DLLEXPORT int step_colony_update(int gpu_index, int iterations) {
    (void)gpu_index;
    if (iterations <= 0) {
        return 1;
    }
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_colony_update: State not initialized.\n");
        return 0;
    }
    uint8_t* tmp_labels = (uint8_t*)malloc((size_t)st->T_cap * sizeof(uint8_t));
    if (!tmp_labels) {
        fprintf(stderr, "[C] step_colony_update: Failed to allocate temporary labels.\n");
        return 0;
    }
    for (int iter = 0; iter < iterations; ++iter) {
        for (int t = 0; t < st->T_act; ++t) {
            if (!st->alive[t]) {
                tmp_labels[t] = st->colony_id[t];
                continue;
            }
            float weights[256] = {0};
            for (int k = 0; k < st->K; ++k) {
                int nb = st->neigh_idx[t * st->K + k];
                if (nb < 0 || nb >= st->T_cap || !st->alive[nb]) {
                    continue;
                }
                uint8_t label = st->colony_id[nb];
                float pher_sum = 0.0f;
                for (int c = 0; c < st->C; ++c) {
                    size_t idx = ((size_t)t * (size_t)st->K + (size_t)k) * (size_t)st->C + (size_t)c;
                    pher_sum += st->pheromone[idx];
                }
                weights[label] += pher_sum;
            }
            uint8_t best_label = st->colony_id[t];
            float best_weight = -1.0f;
            for (int label = 0; label < 256; ++label) {
                if (weights[label] > best_weight) {
                    best_weight = weights[label];
                    best_label = (uint8_t)label;
                }
            }
            tmp_labels[t] = best_label;
        }
        for (int t = 0; t < st->T_act; ++t) {
            st->colony_id[t] = tmp_labels[t];
        }
    }
    free(tmp_labels);
    return 1;
}

DLLEXPORT int step_reproduction(int gpu_index, const float* activity_t, const float* prototypes, int E) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_reproduction: State not initialized.\n");
        return 0;
    }
    if (!activity_t) {
        fprintf(stderr, "[C] step_reproduction: activity pointer is NULL.\n");
        return 0;
    }
    int spawned = 0;
    for (int t = 0; t < st->T_act; ++t) {
        if (!st->alive[t]) {
            continue;
        }
        if (st->nutrient[t] < st->repro_thr_nutrient || activity_t[t] < st->repro_thr_activity) {
            continue;
        }
        int dst = mycel_pop_free(st);
        if (dst < 0) {
            break;
        }
        st->alive[dst] = 1;
        st->nutrient[dst] = st->nutrient[t] * 0.5f;
        st->nutrient[t] *= 0.5f;
        for (int c = 0; c < st->C; ++c) {
            float parent_mood = st->mood[t * st->C + c];
            float mutated = parent_mood + st->repro_mut_sigma * mycel_random_normal();
            st->mood[dst * st->C + c] = mutated;
        }
        st->colony_id[dst] = st->colony_id[t];
        for (int k = 0; k < st->K; ++k) {
            size_t idx = ((size_t)dst * (size_t)st->K + (size_t)k) * (size_t)st->C;
            for (int c = 0; c < st->C; ++c) {
                st->pheromone[idx + (size_t)c] = 0.0f;
            }
        }
        if (prototypes && E > 0) {
            float* proto_mut = (float*)prototypes;
            size_t parent_offset = (size_t)t * (size_t)E;
            size_t child_offset = (size_t)dst * (size_t)E;
            for (int e = 0; e < E; ++e) {
                float parent_val = proto_mut[parent_offset + (size_t)e];
                float mutated = parent_val + st->repro_mut_sigma * mycel_random_normal();
                proto_mut[child_offset + (size_t)e] = mutated;
            }
        }
        spawned += 1;
    }
    if (spawned > 0) {
        mycel_recompute_active_count(st);
        if (!mycel_upload_all_state(st)) {
            fprintf(stderr, "[C] step_reproduction: Failed to synchronize state with GPU.\n");
            return 0;
        }
    }
    return spawned;
}

DLLEXPORT int step_subqg_feedback(int gpu_index, float kappa_nutrient, const float* kappa_mood, int count) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_subqg_feedback: State not initialized.\n");
        return 0;
    }
    if (count > st->C) {
        count = st->C;
    }
    if (kappa_mood && count > 0) {
        memcpy(st->kappa_mood, kappa_mood, (size_t)count * sizeof(float));
    }
    for (int i = count; i < st->C; ++i) {
        st->kappa_mood[i] = 0.0f;
    }
    st->kappa_nutrient = kappa_nutrient;
    for (int t = 0; t < st->T_act; ++t) {
        if (!st->alive[t]) {
            st->subqg_field[t] = 0.0f;
            continue;
        }
        float value = kappa_nutrient * st->nutrient[t];
        for (int c = 0; c < st->C; ++c) {
            value += st->kappa_mood[c] * st->mood[t * st->C + c];
        }
        st->subqg_field[t] = value;
    }
    return 1;
}

DLLEXPORT int step_potential_for_hpio(int gpu_index, const float* mood_weights, int count) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] step_potential_for_hpio: State not initialized.\n");
        return 0;
    }
    for (int t = 0; t < st->T_act; ++t) {
        if (!st->alive[t]) {
            st->potential[t] = 0.0f;
            continue;
        }
        float pot = 0.0f;
        for (int k = 0; k < st->K; ++k) {
            int nb = st->neigh_idx[t * st->K + k];
            if (nb < 0 || nb >= st->T_cap || !st->alive[nb]) {
                continue;
            }
            for (int c = 0; c < st->C; ++c) {
                float weight = (mood_weights && c < count) ? mood_weights[c] : 1.0f;
                size_t idx_t = ((size_t)t * (size_t)st->K + (size_t)k) * (size_t)st->C + (size_t)c;
                size_t idx_nb = ((size_t)nb * (size_t)st->K) * (size_t)st->C + (size_t)c;
                float p_t = st->pheromone[idx_t];
                float p_nb = st->pheromone[idx_nb];
                pot += weight * (p_nb - p_t);
            }
        }
        st->potential[t] = pot;
    }
    return 1;
}

DLLEXPORT int read_pheromone_slice(int gpu_index, int channel, float* out_TK) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] read_pheromone_slice: State not initialized.\n");
        return 0;
    }
    if (!out_TK || channel < 0 || channel >= st->C) {
        fprintf(stderr, "[C] read_pheromone_slice: invalid parameters.\n");
        return 0;
    }
    size_t edge_count = mycel_edge_count(st);
    for (size_t edge = 0; edge < edge_count; ++edge) {
        out_TK[edge] = st->pheromone[edge * (size_t)st->C + (size_t)channel];
    }
    return 1;
}

/* Default-Auflösung für das externe Feld, falls noch nichts gesetzt wurde */
static int g_field_w_default = 256;
static int g_field_h_default = 128;

/* Ermittelt Zielgröße in Bytes für das „externe“ Feld (float32 Raster). */
static int cc_get_external_field_size_bytes(int* out_w, int* out_h) {
    int w = subqg_width  > 0 ? subqg_width  : g_field_w_default;
    int h = subqg_height > 0 ? subqg_height : g_field_h_default;

    /* Falls SubQG-State noch nicht aufgesetzt ist, versuche ihn „leise“ mit Defaults anzulegen. */
    if (!subqg_state_initialized) {
        /* ensure_subqg_state() nutzt intern subqg_initialize_state_batched(); wenn das bei dir
           noch nichts befüllt, ist das ok – wir liefern unten Fallback-Daten zurück. */
        (void)ensure_subqg_state(w, h); /* Fehler hier sind nicht fatal für die Größe */
    }
    if (out_w) *out_w = w;
    if (out_h) *out_h = h;
    /* float32 pro Zelle */
    return w * h * (int)sizeof(float);
}

/* Liest GPU-Feld (falls vorhanden) in dest (float*), Größe muss passen. */
static int cc_read_external_field_gpu(float* dest, int w, int h) {
    if (!dest || w <= 0 || h <= 0) return 0;
    if (!queue) return 0;

    /* Bevorzugt subqg_field_map_buffer (direkte 2D-Karte). */
    if (subqg_field_map_buffer && subqg_field_map_elements == (w * h)) {
        size_t bytes = (size_t)w * (size_t)h * sizeof(float);
        cl_int err = clEnqueueReadBuffer(queue, subqg_field_map_buffer, CL_TRUE, 0, bytes, dest, 0, NULL, NULL);
        if (err == CL_SUCCESS) return 1;
    }

    /* Alternativ: aus Myzel-Pheromonen eine flache Karte schätzen (z. B. Summe Kanal 0) */
    if (g_mycel_state.initialized && g_mycel_state.pheromone) {
        size_t need = (size_t)w * (size_t)h;
        /* einfache Sample-Strategie: nehme die ersten need Werte aus Kanal 0 über K */
        /* robustheitshalber clampen/kacheln */
        size_t edge_stride = (size_t)g_mycel_state.K * (size_t)g_mycel_state.C;
        if (edge_stride == 0) {
            memset(dest, 0, need * sizeof(float));
            return 1;
        }
        for (size_t i = 0; i < need; ++i) {
            /* nehme schlicht einen Wert aus Pheromon[ i % (T_cap*K) ][channel 0] */
            size_t edge = i % ((size_t)g_mycel_state.T_cap * (size_t)g_mycel_state.K);
            size_t base = edge * (size_t)g_mycel_state.C;
            float v = g_mycel_state.pheromone[base + 0];
            dest[i] = v;
        }
        return 1;
    }

    /* Nichts verfügbar → Nullfeld */
    memset(dest, 0, (size_t)w * (size_t)h * sizeof(float));
    return 1;
}

/* Öffentliche API: Size-Probe und Readback. */
DLLEXPORT int subqg_get_dims(int* out_w, int* out_h) {
    if (!subqg_state_initialized || subqg_width <= 0 || subqg_height <= 0) {
        return 0;
    }
    if (out_w) { *out_w = subqg_width; }
    if (out_h) { *out_h = subqg_height; }
    return 1;
}

DLLEXPORT int read_full_pheromone_buffer(void* out_buffer, int out_bytes) {
    int w = 0, h = 0;
    const int need = cc_get_external_field_size_bytes(&w, &h);

    /* PROBE: nur Größe zurückgeben (keine Errors) */
    if (out_buffer == NULL || out_bytes == 0) {
        return need;
    }
    if (out_bytes < need) {
        cc_set_last_error("read_full_pheromone_buffer: buffer too small (need=%d, have=%d)", need, out_bytes);
        return -need;
    }

    float* dst = (float*)out_buffer;
    if (!cc_read_external_field_gpu(dst, w, h)) {
        memset(out_buffer, 0, (size_t)need); /* Fallback: Nullfeld */
    }
    return need; /* tatsächlich geschriebene Bytes */
}

DLLEXPORT int render_frame_to_buffer(int gpu_index, int width, int height,
                                     void* out_buffer_host,
                                     const RenderAgent* agents, int num_agents,
                                     const Vec2f* trail_points, int num_trail_points,
                                     float exposure_scale, float agent_radius,
                                     float trail_thickness, float clip_percentile) {
    MycelState* st = &g_mycel_state;

    if (!out_buffer_host || width <= 0 || height <= 0) {
        fprintf(stderr, "[C] render_frame_to_buffer: invalid dimensions or output buffer.\n");
        return 0;
    }
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] render_frame_to_buffer: state not initialized.\n");
        return 0;
    }

    if (!ensure_subqg_state(width, height)) {
        fprintf(stderr, "[C] render_frame_to_buffer: Failed to ensure SubQG state for %dx%d grid.\n", width, height);
        return 0;
    }

    const int cells = subqg_cell_count;
    if (cells <= 0) {
        fprintf(stderr, "[C] render_frame_to_buffer: invalid SubQG cell count %d.\n", cells);
        return 0;
    }

    if (!subqg_simulation_step_batched(
            gpu_index,
            /*rng_energy*/ NULL, /*rng_phase*/ NULL, /*rng_spin*/ NULL,
            cells,
            /*out_energy*/ NULL, /*out_phase*/ NULL, /*out_interference*/ NULL,
            /*out_node_flag*/ NULL, /*out_spin*/ NULL, /*out_topology*/ NULL,
            /*out_field_map*/ NULL, /*field_map_length*/ 0)) {
        fprintf(stderr, "[C] render_frame_to_buffer: sim step failed\n");
        return 0;
    }

    if (!mycel_ensure_gpu_buffers(st)) {
        fprintf(stderr, "[C] render_frame_to_buffer: Failed to ensure GPU buffers for Mycel state. Falling back to CPU renderer.\n");
        render_frame_cpu(st,
                         (uint8_t*)out_buffer_host,
                         width,
                         height,
                         agents,
                         num_agents,
                         trail_points,
                         num_trail_points,
                         exposure_scale,
                         agent_radius,
                         trail_thickness,
                         clip_percentile);
        return 1;
    }

    fprintf(stderr,
            "[C] render_frame_to_buffer: size=%dx%d agents=%d trails=%d exp=%.3f r=%.2f t=%.2f clip=%.3f\n",
            width, height, num_agents, num_trail_points,
            (double)exposure_scale, (double)agent_radius, (double)trail_thickness,
            (double)clip_percentile);

    GpuSlot* slot = NULL;
    cl_command_queue active_queue = cc_get_slot_queue(gpu_index, 0, &slot);
    cl_context ctx = (slot && slot->context) ? slot->context : context;
    if (!active_queue) { active_queue = queue; }
    cl_device_id dev = (slot && slot->device) ? slot->device : device_id;

    int have_kernels = (render_kernel_img != NULL) || (render_kernel_buf != NULL);
    int use_gpu = (ctx != NULL) && (active_queue != NULL) && have_kernels;
    if (!use_gpu) {
        fprintf(stderr, "[C] render_frame_to_buffer: GPU renderer unavailable, using CPU fallback.\n");
        render_frame_cpu(st,
                         (uint8_t*)out_buffer_host,
                         width,
                         height,
                         agents,
                         num_agents,
                         trail_points,
                         num_trail_points,
                         exposure_scale,
                         agent_radius,
                         trail_thickness,
                         clip_percentile);
        return 1;
    }

    if (g_force_debug_render < 0) {
        const char* env = getenv("MYCEL_DEBUG_RENDER");
        if (env && env[0] != '\0') {
            if (env[0] == '0') {
                g_force_debug_render = 0;
                g_debug_smoke_test_done = 1;
            } else {
                g_force_debug_render = 1;
                fprintf(stderr, "[C] render_frame_to_buffer: MYCEL_DEBUG_RENDER enabled (value='%s').\n", env);
            }
        } else {
            g_force_debug_render = 0;
        }
    }

    int run_debug_kernel = 0;
    if (g_force_debug_render > 0) {
        run_debug_kernel = 1;
    } else if (!g_debug_smoke_test_done) {
        run_debug_kernel = 1;
    }
    if (!render_debug_kernel) {
        run_debug_kernel = 0;
    }

    if (run_debug_kernel) {
        g_debug_smoke_test_done = 1;
        size_t n_px = (size_t)width * (size_t)height;
        cl_int dbg_err = CL_SUCCESS;
        cl_mem out_dev = clCreateBuffer(ctx, CL_MEM_READ_WRITE, n_px * sizeof(cl_uchar4), NULL, &dbg_err);
        if (!out_dev || dbg_err != CL_SUCCESS) {
            fprintf(stderr, "[C] render_frame_to_buffer: Debug buffer alloc failed: %s (%d)\n",
                    clGetErrorString(dbg_err), dbg_err);
        } else {
            dbg_err  = clSetKernelArg(render_debug_kernel, 0, sizeof(cl_mem), &out_dev);
            dbg_err |= clSetKernelArg(render_debug_kernel, 1, sizeof(int), &width);
            dbg_err |= clSetKernelArg(render_debug_kernel, 2, sizeof(int), &height);
            if (dbg_err != CL_SUCCESS) {
                fprintf(stderr, "[C] render_frame_to_buffer: Debug setarg failed: %s (%d)\n",
                        clGetErrorString(dbg_err), dbg_err);
            } else {
                size_t dbg_gws[2] = { (size_t)width, (size_t)height };
                dbg_err = clEnqueueNDRangeKernel(active_queue, render_debug_kernel, 2, NULL, dbg_gws, NULL, 0, NULL, NULL);
                if (dbg_err != CL_SUCCESS) {
                    fprintf(stderr, "[C] render_frame_to_buffer: Debug enqueue failed: %s (%d)\n",
                            clGetErrorString(dbg_err), dbg_err);
                } else {
                    dbg_err = clFinish(active_queue);
                    if (dbg_err != CL_SUCCESS) {
                        fprintf(stderr, "[C] render_frame_to_buffer: Debug clFinish failed: %s (%d)\n",
                                clGetErrorString(dbg_err), dbg_err);
                    } else {
                        dbg_err = clEnqueueReadBuffer(active_queue, out_dev, CL_TRUE, 0,
                                                      n_px * sizeof(cl_uchar4), out_buffer_host,
                                                      0, NULL, NULL);
                        if (dbg_err != CL_SUCCESS) {
                            fprintf(stderr, "[C] render_frame_to_buffer: Debug readback failed: %s (%d)\n",
                                    clGetErrorString(dbg_err), dbg_err);
                        } else {
                            fprintf(stderr, "[C] render_frame_to_buffer: Debug gradient readback complete (%zu px).\n", n_px);
                            if (g_force_debug_render <= 0) {
                                fprintf(stderr, "[C] render_frame_to_buffer: Smoke-test frame used debug gradient output. Set MYCEL_DEBUG_RENDER=0 to skip or =1 to force future debug frames.\n");
                            }
                            clReleaseMemObject(out_dev);
                            return 1;
                        }
                    }
                }
            }
            clReleaseMemObject(out_dev);
        }
        if (g_force_debug_render > 0) {
            return 0;
        }
    }

    int use_safe = 0;
    const char* env_safe = getenv("MYCEL_SAFE_RENDER");
    if (env_safe && env_safe[0] == '1') { use_safe = 1; }

    size_t tile_h = 64;
    const char* env_th = getenv("MYCEL_TILE_H");
    if (env_th && env_th[0] != '\0') {
        long v = strtol(env_th, NULL, 10);
        if (v >= 8 && v <= 512) { tile_h = (size_t)v; }
    }

    int use_buffer = 0;
    const char* env_buf = getenv("MYCEL_RENDER_BUFFER");
    if (env_buf && env_buf[0] == '1') { use_buffer = 1; }

/* --- FIX: Disable tiled dispatch to prevent rendering artifacts on some drivers --- */
/*
if (!use_safe && dev) {
    cl_uint cu = 0;
    if (clGetDeviceInfo(dev, CL_DEVICE_MAX_COMPUTE_UNITS, sizeof(cu), &cu, NULL) == CL_SUCCESS) {
        if (cu > 0 && cu <= 8) {
            use_safe = 1;
            fprintf(stderr, "[C] render_frame_to_buffer: enforcing tiled dispatch for %u compute units.\n", cu);
        }
    }
}
*/
    if (tile_h == 0) { tile_h = 1; }

    float clip_value = clip_percentile;
    if (!isfinite(clip_value) || clip_value <= 0.0f) { clip_value = 1.0f; }
    if (clip_value > 1.0f) { clip_value = 1.0f; }

    cl_int err = CL_SUCCESS;
    cl_mem agents_buf = NULL;
    cl_mem trails_buf = NULL;
    cl_mem out_img = NULL;
    cl_mem out_buf = NULL;
    cl_kernel kernel = NULL;
    int success = 0;
    int using_buffer = use_buffer;
    cl_int width_cl = 0;
    cl_int height_cl = 0;
    cl_int agents_cl = 0;
    cl_int trails_cl = 0;
    cl_float exposure_cl = 0.0f;
    cl_float radius_cl = 0.0f;
    cl_float trail_cl = 0.0f;
    cl_float clip_cl = 0.0f;
    cl_mem out_target = NULL;
    size_t gws_full[2] = {0, 0};
    cl_mem pheromone_cl = NULL;
    cl_int total_cells_cl = 0;
    cl_int active_cells_cl = 0;
    cl_int neighbor_count_cl = 0;
    cl_int channel_count_cl = 0;
    cl_mem subqg_field_cl = NULL;
    cl_int subqg_len_cl = 0;
    cl_int subqg_w_cl = 0;
    cl_int subqg_h_cl = 0;

    if (num_agents < 0) { num_agents = 0; }
    if (num_trail_points < 0) { num_trail_points = 0; }

    size_t agents_bytes = (num_agents > 0 && agents) ? (size_t)num_agents * sizeof(RenderAgent) : 0;
    size_t trails_bytes = (num_trail_points > 0 && trail_points) ? (size_t)num_trail_points * sizeof(Vec2f) : 0;

    if (agents_bytes > 0) {
        agents_buf = clCreateBuffer(ctx, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR,
                                    agents_bytes, (void*)agents, &err);
        if (!agents_buf || err != CL_SUCCESS) {
            fprintf(stderr, "[C] render_frame_to_buffer: failed to upload agents: %s (%d)\n",
                    clGetErrorString(err), err);
            goto gpu_cleanup;
        }
    }
    if (trails_bytes > 0) {
        trails_buf = clCreateBuffer(ctx, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR,
                                    trails_bytes, (void*)trail_points, &err);
        if (!trails_buf || err != CL_SUCCESS) {
            fprintf(stderr, "[C] render_frame_to_buffer: failed to upload trails: %s (%d)\n",
                    clGetErrorString(err), err);
            goto gpu_cleanup;
        }
    }

    if (!using_buffer && render_kernel_img) {
        cl_image_format fmt = { CL_RGBA, CL_UNORM_INT8 };
        cl_image_desc desc;
        memset(&desc, 0, sizeof(desc));
        desc.image_type = CL_MEM_OBJECT_IMAGE2D;
        desc.image_width = (size_t)width;
        desc.image_height = (size_t)height;
        out_img = clCreateImage(ctx, CL_MEM_WRITE_ONLY, &fmt, &desc, NULL, &err);
        if (!out_img || err != CL_SUCCESS) {
            fprintf(stderr, "[C] render_frame_to_buffer: image allocation failed (%s), switching to buffer path.\n",
                    clGetErrorString(err));
            if (out_img) { clReleaseMemObject(out_img); out_img = NULL; }
            using_buffer = 1;
        } else {
            kernel = render_kernel_img;
        }
    }

    if (using_buffer) {
        if (!render_kernel_buf) {
            fprintf(stderr, "[C] render_frame_to_buffer: buffer kernel unavailable.\n");
            goto gpu_cleanup;
        }
        size_t pixel_count = (size_t)width * (size_t)height;
        out_buf = clCreateBuffer(ctx, CL_MEM_WRITE_ONLY,
                                 pixel_count * sizeof(cl_uchar4),
                                 NULL, &err);
        if (!out_buf || err != CL_SUCCESS) {
            fprintf(stderr, "[C] render_frame_to_buffer: output buffer allocation failed: %s (%d)\n",
                    clGetErrorString(err), err);
            goto gpu_cleanup;
        }
        kernel = render_kernel_buf;
    }

    if (!kernel) {
        fprintf(stderr, "[C] render_frame_to_buffer: no render kernel available after setup.\n");
        goto gpu_cleanup;
    }

    width_cl = (cl_int)width;
    height_cl = (cl_int)height;
    agents_cl = (cl_int)num_agents;
    trails_cl = (cl_int)num_trail_points;
    exposure_cl = (cl_float)fmaxf(0.0f, exposure_scale);
    radius_cl = (cl_float)agent_radius;
    trail_cl = (cl_float)trail_thickness;
    clip_cl = (cl_float)clip_value;
    pheromone_cl = st->pheromone_buf;
    total_cells_cl = (cl_int)st->T_cap;
    active_cells_cl = (cl_int)st->T_act;
    neighbor_count_cl = (cl_int)st->K;
    channel_count_cl = (cl_int)st->C;
    subqg_field_cl = subqg_field_map_buffer;
    subqg_len_cl = (cl_int)subqg_field_map_elements;
    subqg_w_cl = (cl_int)((subqg_width > 0) ? subqg_width : width);
    subqg_h_cl = (cl_int)((subqg_height > 0) ? subqg_height : height);

    out_target = using_buffer ? out_buf : out_img;
    err = set_render_kernel_args(kernel, out_target,
                                 agents_buf, agents_cl,
                                 trails_buf, trails_cl,
                                 width_cl, height_cl,
                                 exposure_cl, radius_cl, trail_cl, clip_cl,
                                 pheromone_cl, total_cells_cl, active_cells_cl,
                                 neighbor_count_cl, channel_count_cl,
                                 subqg_field_cl, subqg_len_cl, subqg_w_cl, subqg_h_cl);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] render_frame_to_buffer: failed to set kernel args: %s (%d)\n",
                clGetErrorString(err), err);
        goto gpu_cleanup;
    }

    gws_full[0] = (size_t)width;
    gws_full[1] = (size_t)height;
    if (!use_safe) {
        err = clEnqueueNDRangeKernel(active_queue, kernel, 2, NULL, gws_full, NULL, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] render_frame_to_buffer: kernel launch failed: %s (%d)\n",
                    clGetErrorString(err), err);
            goto gpu_cleanup;
        }
    } else {
        for (size_t y0 = 0; y0 < (size_t)height; y0 += tile_h) {
            size_t this_h = (y0 + tile_h <= (size_t)height) ? tile_h : ((size_t)height - y0);
            size_t gws_tile[2] = { (size_t)width, this_h };
            size_t offsets[2] = { 0, y0 };
            err = clEnqueueNDRangeKernel(active_queue, kernel, 2, offsets, gws_tile, NULL, 0, NULL, NULL);
            if (err != CL_SUCCESS) {
                fprintf(stderr, "[C] render_frame_to_buffer: tiled launch failed at y=%zu: %s (%d)\n",
                        y0, clGetErrorString(err), err);
                goto gpu_cleanup;
            }
            clFlush(active_queue);
        }
    }

    err = clFinish(active_queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] render_frame_to_buffer: clFinish failed: %s (%d)\n",
                clGetErrorString(err), err);
        goto gpu_cleanup;
    }

    if (!using_buffer) {
        size_t origin[3] = {0, 0, 0};
        size_t region[3] = { (size_t)width, (size_t)height, 1 };
        err = clEnqueueReadImage(active_queue, out_img, CL_TRUE, origin, region, 0, 0,
                                 out_buffer_host, 0, NULL, NULL);
    } else {
        size_t bytes = (size_t)width * (size_t)height * sizeof(cl_uchar4);
        err = clEnqueueReadBuffer(active_queue, out_buf, CL_TRUE, 0,
                                  bytes, out_buffer_host, 0, NULL, NULL);
    }
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] render_frame_to_buffer: readback failed: %s (%d)\n",
                clGetErrorString(err), err);
        goto gpu_cleanup;
    }

    success = 1;

gpu_cleanup:
    if (agents_buf) { clReleaseMemObject(agents_buf); }
    if (trails_buf) { clReleaseMemObject(trails_buf); }
    if (out_img) { clReleaseMemObject(out_img); }
    if (out_buf) { clReleaseMemObject(out_buf); }

    if (success) {
        return 1;
    }

    fprintf(stderr, "[C] render_frame_to_buffer: GPU rendering failed, switching to CPU fallback.\n");
    render_frame_cpu(st,
                     (uint8_t*)out_buffer_host,
                     width,
                     height,
                     agents,
                     num_agents,
                     trail_points,
                     num_trail_points,
                     exposure_scale,
                     agent_radius,
                     trail_thickness,
                     clip_percentile);
    return 1;
}

DLLEXPORT int read_nutrient(int gpu_index, float* out_T) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] read_nutrient: State not initialized.\n");
        return 0;
    }
    if (!out_T) {
        return 0;
    }
    memcpy(out_T, st->nutrient, (size_t)st->T_cap * sizeof(float));
    return 1;
}

DLLEXPORT int read_potential(int gpu_index, float* out_T) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] read_potential: State not initialized.\n");
        return 0;
    }
    if (!out_T) {
        return 0;
    }
    memcpy(out_T, st->potential, (size_t)st->T_cap * sizeof(float));
    return 1;
}

DLLEXPORT int read_colonies(int gpu_index, uint8_t* out_T) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] read_colonies: State not initialized.\n");
        return 0;
    }
    if (!out_T) {
        return 0;
    }
    memcpy(out_T, st->colony_id, (size_t)st->T_cap * sizeof(uint8_t));
    return 1;
}

typedef struct MycelPersistHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t T_cap;
    uint32_t C;
    uint32_t K;
    uint32_t T_act;
    uint32_t free_head;
} MycelPersistHeader;

DLLEXPORT int save_mycel_state(int gpu_index, const char* path) {
    (void)gpu_index;
    MycelState* st = &g_mycel_state;
    if (!mycel_check_initialized(st)) {
        fprintf(stderr, "[C] save_mycel_state: State not initialized.\n");
        return 0;
    }
    if (!path) {
        fprintf(stderr, "[C] save_mycel_state: path is NULL.\n");
        return 0;
    }
    FILE* f = fopen(path, "wb");
    if (!f) {
        fprintf(stderr, "[C] save_mycel_state: Unable to open %s for writing.\n", path);
        return 0;
    }
    MycelPersistHeader header;
    header.magic = 0x4D59434C;
    header.version = 1;
    header.T_cap = (uint32_t)st->T_cap;
    header.C = (uint32_t)st->C;
    header.K = (uint32_t)st->K;
    header.T_act = (uint32_t)st->T_act;
    header.free_head = (uint32_t)st->free_head;
    size_t edge_count = mycel_edge_count(st);
    size_t pher_count = mycel_pheromone_count(st);
    if (fwrite(&header, sizeof(header), 1, f) != 1) {
        fclose(f);
        return 0;
    }
    fwrite(st->alive, sizeof(uint8_t), (size_t)st->T_cap, f);
    fwrite(st->colony_id, sizeof(uint8_t), (size_t)st->T_cap, f);
    fwrite(st->free_list, sizeof(int), (size_t)st->T_cap, f);
    fwrite(st->nutrient, sizeof(float), (size_t)st->T_cap, f);
    fwrite(st->mood, sizeof(float), (size_t)st->T_cap * (size_t)st->C, f);
    fwrite(st->reinforce_gain, sizeof(float), (size_t)st->C, f);
    fwrite(st->kappa_mood, sizeof(float), (size_t)st->C, f);
    fwrite(st->neigh_idx, sizeof(int), edge_count, f);
    fwrite(st->decay, sizeof(float), edge_count, f);
    fwrite(st->diffu, sizeof(float), edge_count, f);
    fwrite(st->pheromone, sizeof(float), pher_count, f);
    fwrite(st->potential, sizeof(float), (size_t)st->T_cap, f);
    fwrite(st->subqg_field, sizeof(float), (size_t)st->T_cap, f);
    float extras[3] = {st->repro_thr_nutrient, st->repro_thr_activity, st->repro_mut_sigma};
    fwrite(extras, sizeof(float), 3, f);
    float extras2[2] = {st->decay_default, st->diffu_default};
    fwrite(extras2, sizeof(float), 2, f);
    fwrite(&st->nutrient_recovery, sizeof(float), 1, f);
    fwrite(&st->kappa_nutrient, sizeof(float), 1, f);
    fclose(f);
    return 1;
}

DLLEXPORT int load_mycel_state(int gpu_index, const char* path) {
    (void)gpu_index;
    if (!path) {
        fprintf(stderr, "[C] load_mycel_state: path is NULL.\n");
        return 0;
    }
    FILE* f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "[C] load_mycel_state: Unable to open %s for reading.\n", path);
        return 0;
    }
    MycelPersistHeader header;
    if (fread(&header, sizeof(header), 1, f) != 1) {
        fclose(f);
        fprintf(stderr, "[C] load_mycel_state: Failed to read header.\n");
        return 0;
    }
    if (header.magic != 0x4D59434C || header.version != 1) {
        fclose(f);
        fprintf(stderr, "[C] load_mycel_state: Invalid file format.\n");
        return 0;
    }
    if (!mycel_initialize(&g_mycel_state, (int)header.T_cap, (int)header.C, (int)header.K)) {
        fclose(f);
        fprintf(stderr, "[C] load_mycel_state: Failed to allocate state.\n");
        return 0;
    }
    MycelState* st = &g_mycel_state;
    st->T_act = (int)header.T_act;
    st->free_head = (int)header.free_head;
    size_t edge_count = mycel_edge_count(st);
    size_t pher_count = mycel_pheromone_count(st);
    fread(st->alive, sizeof(uint8_t), (size_t)st->T_cap, f);
    fread(st->colony_id, sizeof(uint8_t), (size_t)st->T_cap, f);
    fread(st->free_list, sizeof(int), (size_t)st->T_cap, f);
    fread(st->nutrient, sizeof(float), (size_t)st->T_cap, f);
    fread(st->mood, sizeof(float), (size_t)st->T_cap * (size_t)st->C, f);
    fread(st->reinforce_gain, sizeof(float), (size_t)st->C, f);
    fread(st->kappa_mood, sizeof(float), (size_t)st->C, f);
    fread(st->neigh_idx, sizeof(int), edge_count, f);
    fread(st->decay, sizeof(float), edge_count, f);
    fread(st->diffu, sizeof(float), edge_count, f);
    fread(st->pheromone, sizeof(float), pher_count, f);
    fread(st->potential, sizeof(float), (size_t)st->T_cap, f);
    fread(st->subqg_field, sizeof(float), (size_t)st->T_cap, f);
    float extras[3];
    fread(extras, sizeof(float), 3, f);
    st->repro_thr_nutrient = extras[0];
    st->repro_thr_activity = extras[1];
    st->repro_mut_sigma = extras[2];
    float extras2[2];
    fread(extras2, sizeof(float), 2, f);
    st->decay_default = extras2[0];
    st->diffu_default = extras2[1];
    fread(&st->nutrient_recovery, sizeof(float), 1, f);
    fread(&st->kappa_nutrient, sizeof(float), 1, f);
    if (!mycel_upload_all_state(st)) {
        fprintf(stderr, "[C] load_mycel_state: Failed to synchronize loaded state with GPU.\n");
        fclose(f);
        mycel_free_state(st);
        return 0;
    }
    fclose(f);
    return 1;
}

DLLEXPORT void subqg_set_deterministic_mode(int enabled, uint64_t seed) {
    if (enabled) {
        subqg_deterministic_mode = 1;
        subqg_seed_rng_state(seed);
    } else {
        subqg_deterministic_mode = 0;
        if (seed != 0) {
            subqg_seed_rng_state(seed);
        }
    }
}

DLLEXPORT void subqg_release_state(int gpu_index) {
    (void)gpu_index;
    release_subqg_resources();
}

// ===========================================================================
// NEUE FUNKTION HIER EINFÜGEN
// ===========================================================================

DLLEXPORT void subqg_set_params(float noise_level, float threshold) {
    // Setzt den globalen Rauschpegel für die SubQG-Simulation.
    // Ein Wert > 0 ist notwendig, damit die Wellenbewegung startet.
    if (noise_level >= 0.0f) {
        subqg_noise_level = noise_level;
    }

    // Setzt den Schwellenwert für die Simulation.
    if (threshold >= 0.0f) {
        subqg_threshold = threshold;
    }

    // Gibt eine Bestätigung auf der Konsole aus, um das Debugging zu erleichtern.
    printf("[C] SubQG-Parameter gesetzt: Noise Level = %.4f, Threshold = %.4f\n",
           subqg_noise_level, subqg_threshold);
}

DLLEXPORT int execute_shor_gpu(int gpu_index, int modulus_N, int base_a,
                               int* out_period_estimate,
                               float* out_control_distribution, int distribution_length) {
    (void)gpu_index;
    if (modulus_N <= 1 || base_a <= 1) {
        fprintf(stderr, "[C] Shor: Invalid modulus (%d) or base (%d).\n", modulus_N, base_a);
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }

    double log_base2 = log((double)modulus_N) / log(2.0);
    int num_work = (int)ceil(log_base2);
    if (num_work < 1) { num_work = 1; }
    double log_sq = log((double)modulus_N * (double)modulus_N) / log(2.0);
    int num_control = (int)ceil(log_sq);
    if (num_control < num_work + 1) { num_control = num_work + 1; }
    size_t control_dimension = (size_t)1 << num_control;
    if (distribution_length > 0 && (size_t)distribution_length < control_dimension) {
        fprintf(stderr, "[C] Shor: Provided distribution buffer too small (have %d need %zu).\n",
                distribution_length, control_dimension);
        return 0;
    }

    QuantumStateGPU state = {0};
    int total_qubits = num_control + num_work;
    if (!quantum_allocate_state(total_qubits, &state)) {
        return 0;
    }

    int success = 0;
    float* control_probs = (float*)calloc(control_dimension, sizeof(float));
    float* full_probs = NULL;
    cl_mem probability_buffer = NULL;
    size_t probability_bytes = 0;
    cl_int err = CL_SUCCESS;
    int best_index = 0;
    float best_prob = -1.0f;
    if (!control_probs) {
        fprintf(stderr, "[C] Shor: Failed to allocate control distribution host buffer.\n");
        goto cleanup;
    }

    if (!quantum_apply_pauli_x(&state, 0)) { goto cleanup; }
    if (!quantum_prepare_uniform_superposition(&state, num_control, num_work)) { goto cleanup; }
    if (!quantum_apply_modular_exponentiation(&state, num_control, num_work, base_a, modulus_N)) { goto cleanup; }
    if (!quantum_inverse_qft(&state, num_work, num_control)) { goto cleanup; }

    if (!quantum_compute_probabilities_gpu(&state, &probability_buffer)) { goto cleanup; }
    probability_bytes = state.dimension * sizeof(cl_float);
    full_probs = (float*)malloc(probability_bytes);
    if (!full_probs) {
        fprintf(stderr, "[C] Shor: Failed to allocate host probability buffer (%zu bytes).\n", probability_bytes);
        goto cleanup;
    }
    err = clEnqueueReadBuffer(queue, probability_buffer, CL_TRUE, 0, probability_bytes, full_probs, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Shor: Failed to read probability buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    for (size_t idx = 0; idx < state.dimension; ++idx) {
        size_t control_state = idx >> num_work;
        control_probs[control_state] += full_probs[idx];
    }

    for (size_t i = 0; i < control_dimension; ++i) {
        if (control_probs[i] > best_prob) {
            best_prob = control_probs[i];
            best_index = (int)i;
        }
    }

    if (out_control_distribution) {
        memcpy(out_control_distribution, control_probs, control_dimension * sizeof(float));
    }

    if (out_period_estimate) {
        int estimated_order = 0;
        if (best_index != 0) {
            double approx = (double)best_index / (double)control_dimension;
            double tolerance = 1.0 / (double)(1ULL << (num_control + 1));
            for (int candidate = 1; candidate <= modulus_N; ++candidate) {
                double scaled = approx * (double)candidate;
                double numerator = nearbyint(scaled);
                double diff = fabs(approx - numerator / (double)candidate);
                if (diff < tolerance) {
                    uint64_t pow_mod = host_modexp_uint64((uint64_t)base_a, (uint64_t)candidate, (uint64_t)modulus_N);
                    if (pow_mod == 1ULL) {
                        estimated_order = candidate;
                        break;
                    }
                }
            }
        }
        *out_period_estimate = estimated_order;
    }

    success = 1;

cleanup:
    if (control_probs) { free(control_probs); }
    if (full_probs) { free(full_probs); }
    if (probability_buffer) { clReleaseMemObject(probability_buffer); }
    quantum_release_state(&state);
    return success;
}

DLLEXPORT int execute_grover_gpu(int gpu_index, int num_qubits, int iterations,
                                 uint64_t marked_mask, uint64_t marked_value,
                                 int* out_marked_state,
                                 float* out_distribution, int distribution_length) {
    (void)gpu_index;
    if (num_qubits <= 0 || iterations <= 0) {
        fprintf(stderr, "[C] Grover: Invalid qubit count (%d) or iteration count (%d).\n", num_qubits, iterations);
        return 0;
    }
    size_t dimension = (size_t)1 << num_qubits;
    if (out_distribution && distribution_length > 0 && (size_t)distribution_length < dimension) {
        fprintf(stderr, "[C] Grover: Distribution buffer too small (have %d need %zu).\n",
                distribution_length, dimension);
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }

    QuantumStateGPU state = {0};
    if (!quantum_allocate_state(num_qubits, &state)) { return 0; }

    int success = 0;
    float* host_distribution = NULL;
    cl_mem probabilities = NULL;
    size_t bytes = 0;
    cl_int err = CL_SUCCESS;

    if (!quantum_prepare_uniform_superposition(&state, num_qubits, 0)) { goto cleanup; }

    for (int iter = 0; iter < iterations; ++iter) {
        if (!quantum_apply_grover_oracle(&state, marked_mask, marked_value)) { goto cleanup; }
        if (!quantum_apply_grover_diffusion(&state)) { goto cleanup; }
    }

    if (!quantum_compute_probabilities_gpu(&state, &probabilities)) { goto cleanup; }
    bytes = dimension * sizeof(cl_float);
    host_distribution = (float*)malloc(bytes);
    if (!host_distribution) {
        fprintf(stderr, "[C] Grover: Failed to allocate host distribution buffer (%zu bytes).\n", bytes);
        goto cleanup;
    }
    err = clEnqueueReadBuffer(queue, probabilities, CL_TRUE, 0, bytes, host_distribution, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Grover: Failed to read probability buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    if (out_distribution) {
        memcpy(out_distribution, host_distribution, bytes);
    }

    if (out_marked_state) {
        int best_index = 0;
        float best_prob = -1.0f;
        for (size_t i = 0; i < dimension; ++i) {
            if (host_distribution[i] > best_prob) {
                best_prob = host_distribution[i];
                best_index = (int)i;
            }
        }
        *out_marked_state = best_index;
    }

    success = 1;

cleanup:
    if (host_distribution) { free(host_distribution); }
    if (probabilities) { clReleaseMemObject(probabilities); }
    quantum_release_state(&state);
    return success;
}

DLLEXPORT int execute_vqe_gpu(int gpu_index, int num_qubits, int ansatz_layers,
                              const float* parameters, int num_parameters,
                              const PauliZTerm* hamiltonian_terms, int num_terms,
                              float* out_energy, float* out_gradients) {
    (void)gpu_index;
    if (num_qubits <= 0 || ansatz_layers <= 0 || num_terms <= 0 || !parameters || !hamiltonian_terms) {
        fprintf(stderr, "[C] VQE: Invalid configuration.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }

    QuantumStateGPU state = {0};
    if (!quantum_allocate_state(num_qubits, &state)) { return 0; }

    int success = 0;
    float energy_value = 0.0f;

    if (!quantum_apply_vqe_ansatz(&state, num_qubits, ansatz_layers, parameters, num_parameters)) {
        goto cleanup;
    }
    if (!quantum_compute_pauli_z_energy(&state, hamiltonian_terms, num_terms, &energy_value)) {
        goto cleanup;
    }
    if (out_energy) { *out_energy = energy_value; }

    if (out_gradients) {
        float* shifted_params = (float*)malloc(num_parameters * sizeof(float));
        if (!shifted_params) {
            fprintf(stderr, "[C] VQE: Failed to allocate gradient workspace.\n");
            goto cleanup;
        }
        memcpy(shifted_params, parameters, num_parameters * sizeof(float));
        for (int i = 0; i < num_parameters; ++i) {
            shifted_params[i] = parameters[i] + (float)(M_PI / 2.0);
            if (!quantum_apply_vqe_ansatz(&state, num_qubits, ansatz_layers, shifted_params, num_parameters)) {
                free(shifted_params);
                goto cleanup;
            }
            float forward = 0.0f;
            if (!quantum_compute_pauli_z_energy(&state, hamiltonian_terms, num_terms, &forward)) {
                free(shifted_params);
                goto cleanup;
            }
            shifted_params[i] = parameters[i] - (float)(M_PI / 2.0);
            if (!quantum_apply_vqe_ansatz(&state, num_qubits, ansatz_layers, shifted_params, num_parameters)) {
                free(shifted_params);
                goto cleanup;
            }
            float backward = 0.0f;
            if (!quantum_compute_pauli_z_energy(&state, hamiltonian_terms, num_terms, &backward)) {
                free(shifted_params);
                goto cleanup;
            }
            out_gradients[i] = 0.5f * (forward - backward);
            shifted_params[i] = parameters[i];
        }
        free(shifted_params);
        // Re-prepare original state after gradient evaluations
        if (!quantum_apply_vqe_ansatz(&state, num_qubits, ansatz_layers, parameters, num_parameters)) {
            goto cleanup;
        }
    }

    success = 1;

cleanup:
    quantum_release_state(&state);
    return success;
}

DLLEXPORT int execute_vqe_gradients_parallel_gpu(int gpu_index, int num_qubits, int ansatz_layers,
                                                 const float* parameters, int num_parameters,
                                                 const PauliZTerm* hamiltonian_terms, int num_terms,
                                                 float* out_energy, float* out_gradients) {
    (void)gpu_index;
    if (num_qubits <= 0 || ansatz_layers <= 0 || num_terms <= 0 || !parameters || !hamiltonian_terms ||
        num_parameters <= 0 || !out_gradients) {
        fprintf(stderr, "[C] VQE: Invalid configuration for parallel gradient computation.\n");
        return 0;
    }
    if (num_parameters > INT_MAX || num_terms > INT_MAX || ansatz_layers > INT_MAX) {
        fprintf(stderr, "[C] VQE: Parameter or term count exceeds supported limits.\n");
        return 0;
    }
    if (num_qubits < 0 || num_qubits >= (int)(sizeof(size_t) * 8)) {
        fprintf(stderr, "[C] VQE: Qubit count %d is not supported for parallel gradients.\n", num_qubits);
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (!queue) {
        fprintf(stderr, "[C] VQE: Command queue unavailable for gradient launch.\n");
        return 0;
    }

    size_t dimension = (size_t)1 << num_qubits;
    size_t params_bytes = (size_t)num_parameters * sizeof(float);
    size_t mask_bytes = (size_t)num_terms * sizeof(cl_ulong);
    size_t coeff_bytes = (size_t)num_terms * sizeof(float);
    if ((size_t)num_parameters > SIZE_MAX / sizeof(float) ||
        (size_t)num_terms > SIZE_MAX / sizeof(cl_ulong) ||
        dimension > SIZE_MAX / sizeof(cl_float2)) {
        fprintf(stderr, "[C] VQE: Size computation overflow for gradient buffers.\n");
        return 0;
    }
    size_t state_bytes_per = dimension * sizeof(cl_float2);
    if (state_bytes_per > 0 && (size_t)num_parameters > SIZE_MAX / state_bytes_per) {
        fprintf(stderr, "[C] VQE: Gradient workspace would exceed addressable memory.\n");
        return 0;
    }
    size_t workspace_bytes = state_bytes_per * (size_t)num_parameters;

    cl_mem param_buf = NULL;
    cl_mem mask_buf = NULL;
    cl_mem coeff_buf = NULL;
    cl_mem workspace_buf = NULL;
    cl_mem grad_buf = NULL;
    cl_ulong* mask_host = NULL;
    float* coeff_host = NULL;
    int success = 0;
    cl_int err = CL_SUCCESS;
    int arg = 0;
    cl_int num_params_cl = 0;
    cl_int num_qubits_cl = 0;
    cl_int ansatz_layers_cl = 0;
    cl_int num_terms_cl = 0;
    size_t global = 0;

    mask_host = (cl_ulong*)malloc(mask_bytes);
    coeff_host = (float*)malloc(coeff_bytes);
    if (!mask_host || !coeff_host) {
        fprintf(stderr, "[C] VQE: Failed to allocate host buffers for Hamiltonian terms.\n");
        goto cleanup;
    }
    for (int t = 0; t < num_terms; ++t) {
        mask_host[t] = (cl_ulong)hamiltonian_terms[t].z_mask;
        coeff_host[t] = hamiltonian_terms[t].coefficient;
    }

    param_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, params_bytes, NULL, &err);
    if (err != CL_SUCCESS || !param_buf) {
        fprintf(stderr, "[C] VQE: Failed to allocate parameter buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, param_buf, CL_TRUE, 0, params_bytes, parameters, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] VQE: Failed to upload parameters: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    mask_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, mask_bytes, NULL, &err);
    if (err != CL_SUCCESS || !mask_buf) {
        fprintf(stderr, "[C] VQE: Failed to allocate mask buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, mask_buf, CL_TRUE, 0, mask_bytes, mask_host, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] VQE: Failed to upload mask buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    coeff_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, coeff_bytes, NULL, &err);
    if (err != CL_SUCCESS || !coeff_buf) {
        fprintf(stderr, "[C] VQE: Failed to allocate coefficient buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, coeff_buf, CL_TRUE, 0, coeff_bytes, coeff_host, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] VQE: Failed to upload coefficient buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    workspace_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, workspace_bytes, NULL, &err);
    if (err != CL_SUCCESS || !workspace_buf) {
        fprintf(stderr, "[C] VQE: Failed to allocate gradient workspace: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    grad_buf = clCreateBuffer(context, CL_MEM_WRITE_ONLY, params_bytes, NULL, &err);
    if (err != CL_SUCCESS || !grad_buf) {
        fprintf(stderr, "[C] VQE: Failed to allocate gradient output buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    arg = 0;
    num_params_cl = (cl_int)num_parameters;
    num_qubits_cl = (cl_int)num_qubits;
    ansatz_layers_cl = (cl_int)ansatz_layers;
    num_terms_cl = (cl_int)num_terms;

    err  = clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_mem), &grad_buf);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_mem), &param_buf);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_int), &num_params_cl);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_int), &num_qubits_cl);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_int), &ansatz_layers_cl);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_mem), &mask_buf);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_mem), &coeff_buf);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_int), &num_terms_cl);
    err |= clSetKernelArg(quantum_vqe_gradient_kernel, arg++, sizeof(cl_mem), &workspace_buf);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] VQE: Failed to set gradient kernel arguments: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    global = (size_t)num_parameters;
    err = ENQUEUE_KERNEL_PROFILED(quantum_vqe_gradient_kernel, 1, &global, NULL, "vqe_gradient_batch");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] VQE: Failed to enqueue gradient kernel: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueReadBuffer(queue, grad_buf, CL_TRUE, 0, params_bytes, out_gradients, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] VQE: Failed to read gradient results: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    success = 1;

    if (out_energy) {
        if (!execute_vqe_gpu(gpu_index, num_qubits, ansatz_layers, parameters, num_parameters,
                             hamiltonian_terms, num_terms, out_energy, NULL)) {
            success = 0;
        }
    }

cleanup:
    if (!success && out_gradients) {
        memset(out_gradients, 0, params_bytes);
    }
    if (grad_buf) { clReleaseMemObject(grad_buf); }
    if (workspace_buf) { clReleaseMemObject(workspace_buf); }
    if (coeff_buf) { clReleaseMemObject(coeff_buf); }
    if (mask_buf) { clReleaseMemObject(mask_buf); }
    if (param_buf) { clReleaseMemObject(param_buf); }
    if (mask_host) { free(mask_host); }
    if (coeff_host) { free(coeff_host); }
    return success;
}

DLLEXPORT int compute_qualia_resonance_gpu(int gpu_index, int signal_count,
                                           const float* gradient_signal,
                                           const float* field_flux_signal,
                                           const float* coherence_signal,
                                           const float* novelty_signal,
                                           float mood_bias,
                                           float harmony_gain,
                                           float* resonance_vector_out,
                                           float* resonance_field_out) {
    size_t bytes = (size_t)((signal_count > 0) ? signal_count : 0) * sizeof(float);
    if (signal_count <= 0 || !gradient_signal || !field_flux_signal ||
        !coherence_signal || !novelty_signal || !resonance_vector_out) {
        fprintf(stderr, "[C] Qualia Resonator: Invalid inputs.\n");
        if (resonance_vector_out) {
            for (int i = 0; i < 4; ++i) { resonance_vector_out[i] = 0.0f; }
        }
        if (resonance_field_out && bytes > 0) {
            memset(resonance_field_out, 0, bytes);
        }
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        if (resonance_field_out && bytes > 0) { memset(resonance_field_out, 0, bytes); }
        for (int i = 0; i < 4; ++i) { resonance_vector_out[i] = 0.0f; }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem grad_buf = NULL;
    cl_mem flux_buf = NULL;
    cl_mem coherence_buf = NULL;
    cl_mem novelty_buf = NULL;
    cl_mem field_buf = NULL;
    cl_mem vector_buf = NULL;
    const size_t vector_bytes = 4 * sizeof(float);
    float zero_vec[4] = {0.0f, 0.0f, 0.0f, 0.0f};
    cl_int count_cl = 0;
    cl_float mood_bias_cl = 0.0f;
    cl_float harmony_gain_cl = 0.0f;
    size_t global = 0;
    int success = 0;

    grad_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !grad_buf) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to allocate gradient buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    flux_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !flux_buf) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to allocate flux buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    coherence_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !coherence_buf) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to allocate coherence buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    novelty_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !novelty_buf) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to allocate novelty buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    field_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, bytes, NULL, &err);
    if (err != CL_SUCCESS || !field_buf) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to allocate resonance field buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    vector_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, vector_bytes, NULL, &err);
    if (err != CL_SUCCESS || !vector_buf) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to allocate resonance vector buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueWriteBuffer(queue, grad_buf, CL_TRUE, 0, bytes, gradient_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to upload gradient signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, flux_buf, CL_TRUE, 0, bytes, field_flux_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to upload field flux signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, coherence_buf, CL_TRUE, 0, bytes, coherence_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to upload coherence signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, novelty_buf, CL_TRUE, 0, bytes, novelty_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to upload novelty signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, vector_buf, CL_TRUE, 0, vector_bytes, zero_vec, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to clear resonance accumulator: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    count_cl = (cl_int)signal_count;
    mood_bias_cl = (cl_float)mood_bias;
    harmony_gain_cl = (cl_float)harmony_gain;

    err  = clSetKernelArg(qualia_resonator_kernel, 0, sizeof(cl_mem), &grad_buf);
    err |= clSetKernelArg(qualia_resonator_kernel, 1, sizeof(cl_mem), &flux_buf);
    err |= clSetKernelArg(qualia_resonator_kernel, 2, sizeof(cl_mem), &coherence_buf);
    err |= clSetKernelArg(qualia_resonator_kernel, 3, sizeof(cl_mem), &novelty_buf);
    err |= clSetKernelArg(qualia_resonator_kernel, 4, sizeof(cl_mem), &field_buf);
    err |= clSetKernelArg(qualia_resonator_kernel, 5, sizeof(cl_mem), &vector_buf);
    err |= clSetKernelArg(qualia_resonator_kernel, 6, sizeof(cl_int), &count_cl);
    err |= clSetKernelArg(qualia_resonator_kernel, 7, sizeof(cl_float), &mood_bias_cl);
    err |= clSetKernelArg(qualia_resonator_kernel, 8, sizeof(cl_float), &harmony_gain_cl);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    global = (size_t)signal_count;
    err = ENQUEUE_KERNEL_PROFILED(qualia_resonator_kernel, 1, &global, NULL, "qualia_resonator");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Kernel launch failed: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueReadBuffer(queue, vector_buf, CL_TRUE, 0, vector_bytes, resonance_vector_out, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Qualia Resonator: Failed to read resonance vector: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    if (resonance_field_out) {
        err = clEnqueueReadBuffer(queue, field_buf, CL_TRUE, 0, bytes, resonance_field_out, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] Qualia Resonator: Failed to read resonance field: %s (%d)\n", clGetErrorString(err), err);
            goto cleanup;
        }
    }
    if (signal_count > 0) {
        for (int i = 0; i < 4; ++i) {
            resonance_vector_out[i] /= (float)signal_count;
        }
    }

    success = 1;

cleanup:
    if (!success) {
        for (int i = 0; i < 4; ++i) { resonance_vector_out[i] = 0.0f; }
        if (resonance_field_out && bytes > 0) {
            memset(resonance_field_out, 0, bytes);
        }
    }
    if (vector_buf) { clReleaseMemObject(vector_buf); }
    if (field_buf) { clReleaseMemObject(field_buf); }
    if (novelty_buf) { clReleaseMemObject(novelty_buf); }
    if (coherence_buf) { clReleaseMemObject(coherence_buf); }
    if (flux_buf) { clReleaseMemObject(flux_buf); }
    if (grad_buf) { clReleaseMemObject(grad_buf); }
    return success;
}

DLLEXPORT int compute_intuition_precognition_gpu(int gpu_index, int signal_count,
                                                 const float* pheromone_signal,
                                                 const float* field_signal,
                                                 const float* quantum_signal,
                                                 float sensitivity,
                                                 float anticipation_gain,
                                                 float* intuition_vector_out,
                                                 float* foresight_field_out) {
    size_t bytes = (size_t)((signal_count > 0) ? signal_count : 0) * sizeof(float);
    if (signal_count <= 0 || !pheromone_signal || !field_signal || !quantum_signal || !intuition_vector_out) {
        fprintf(stderr, "[C] Intuition Catalyst: Invalid inputs.\n");
        if (intuition_vector_out) {
            for (int i = 0; i < 3; ++i) { intuition_vector_out[i] = 0.0f; }
        }
        if (foresight_field_out && bytes > 0) {
            memset(foresight_field_out, 0, bytes);
        }
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        for (int i = 0; i < 3; ++i) { intuition_vector_out[i] = 0.0f; }
        if (foresight_field_out && bytes > 0) { memset(foresight_field_out, 0, bytes); }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem pher_buf = NULL;
    cl_mem field_buf = NULL;
    cl_mem quantum_buf = NULL;
    cl_mem foresight_buf = NULL;
    cl_mem vector_buf = NULL;
    const size_t vector_bytes = 3 * sizeof(float);
    float zero_vec[3] = {0.0f, 0.0f, 0.0f};
    cl_int count_cl = 0;
    cl_float sensitivity_cl = 0.0f;
    cl_float anticipation_cl = 0.0f;
    size_t global = 0;
    int success = 0;

    pher_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !pher_buf) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to allocate pheromone buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    field_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !field_buf) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to allocate field buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    quantum_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !quantum_buf) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to allocate quantum buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    foresight_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, bytes, NULL, &err);
    if (err != CL_SUCCESS || !foresight_buf) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to allocate foresight buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    vector_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, vector_bytes, NULL, &err);
    if (err != CL_SUCCESS || !vector_buf) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to allocate intuition vector buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueWriteBuffer(queue, pher_buf, CL_TRUE, 0, bytes, pheromone_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to upload pheromone signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, field_buf, CL_TRUE, 0, bytes, field_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to upload field signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, quantum_buf, CL_TRUE, 0, bytes, quantum_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to upload quantum signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, vector_buf, CL_TRUE, 0, vector_bytes, zero_vec, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to clear intuition accumulator: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    count_cl = (cl_int)signal_count;
    sensitivity_cl = (cl_float)sensitivity;
    anticipation_cl = (cl_float)anticipation_gain;

    err  = clSetKernelArg(intuition_precognition_kernel, 0, sizeof(cl_mem), &pher_buf);
    err |= clSetKernelArg(intuition_precognition_kernel, 1, sizeof(cl_mem), &field_buf);
    err |= clSetKernelArg(intuition_precognition_kernel, 2, sizeof(cl_mem), &quantum_buf);
    err |= clSetKernelArg(intuition_precognition_kernel, 3, sizeof(cl_mem), &foresight_buf);
    err |= clSetKernelArg(intuition_precognition_kernel, 4, sizeof(cl_mem), &vector_buf);
    err |= clSetKernelArg(intuition_precognition_kernel, 5, sizeof(cl_int), &count_cl);
    err |= clSetKernelArg(intuition_precognition_kernel, 6, sizeof(cl_float), &sensitivity_cl);
    err |= clSetKernelArg(intuition_precognition_kernel, 7, sizeof(cl_float), &anticipation_cl);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    global = (size_t)signal_count;
    err = ENQUEUE_KERNEL_PROFILED(intuition_precognition_kernel, 1, &global, NULL, "intuition_precognition");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Kernel launch failed: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueReadBuffer(queue, vector_buf, CL_TRUE, 0, vector_bytes, intuition_vector_out, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Intuition Catalyst: Failed to read intuition vector: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    if (foresight_field_out) {
        err = clEnqueueReadBuffer(queue, foresight_buf, CL_TRUE, 0, bytes, foresight_field_out, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] Intuition Catalyst: Failed to read foresight field: %s (%d)\n", clGetErrorString(err), err);
            goto cleanup;
        }
    }
    if (signal_count > 0) {
        for (int i = 0; i < 3; ++i) {
            intuition_vector_out[i] /= (float)signal_count;
        }
    }

    success = 1;

cleanup:
    if (!success) {
        for (int i = 0; i < 3; ++i) { intuition_vector_out[i] = 0.0f; }
        if (foresight_field_out && bytes > 0) { memset(foresight_field_out, 0, bytes); }
    }
    if (vector_buf) { clReleaseMemObject(vector_buf); }
    if (foresight_buf) { clReleaseMemObject(foresight_buf); }
    if (quantum_buf) { clReleaseMemObject(quantum_buf); }
    if (field_buf) { clReleaseMemObject(field_buf); }
    if (pher_buf) { clReleaseMemObject(pher_buf); }
    return success;
}

DLLEXPORT int compute_context_resonance_gpu(int gpu_index, int signal_count,
                                            const float* stimulus_signal,
                                            const float* response_signal,
                                            const float* valence_signal,
                                            float recency_bias,
                                            float significance_scale,
                                            float* context_vector_out,
                                            float* context_field_out) {
    size_t bytes = (size_t)((signal_count > 0) ? signal_count : 0) * sizeof(float);
    if (signal_count <= 0 || !stimulus_signal || !response_signal || !valence_signal || !context_vector_out) {
        fprintf(stderr, "[C] Context Bridge: Invalid inputs.\n");
        if (context_vector_out) {
            for (int i = 0; i < 3; ++i) { context_vector_out[i] = 0.0f; }
        }
        if (context_field_out && bytes > 0) {
            memset(context_field_out, 0, bytes);
        }
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        for (int i = 0; i < 3; ++i) { context_vector_out[i] = 0.0f; }
        if (context_field_out && bytes > 0) { memset(context_field_out, 0, bytes); }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem stimulus_buf = NULL;
    cl_mem response_buf = NULL;
    cl_mem valence_buf = NULL;
    cl_mem context_field_buf = NULL;
    cl_mem context_vector_buf = NULL;
    const size_t vector_bytes = 3 * sizeof(float);
    float zero_vec[3] = {0.0f, 0.0f, 0.0f};
    cl_int count_cl = 0;
    cl_float recency_bias_cl = 0.0f;
    cl_float significance_scale_cl = 0.0f;
    size_t global = 0;
    int success = 0;

    stimulus_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !stimulus_buf) {
        fprintf(stderr, "[C] Context Bridge: Failed to allocate stimulus buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    response_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !response_buf) {
        fprintf(stderr, "[C] Context Bridge: Failed to allocate response buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    valence_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !valence_buf) {
        fprintf(stderr, "[C] Context Bridge: Failed to allocate valence buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    context_field_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, bytes, NULL, &err);
    if (err != CL_SUCCESS || !context_field_buf) {
        fprintf(stderr, "[C] Context Bridge: Failed to allocate context field buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    context_vector_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, vector_bytes, NULL, &err);
    if (err != CL_SUCCESS || !context_vector_buf) {
        fprintf(stderr, "[C] Context Bridge: Failed to allocate context vector buffer: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueWriteBuffer(queue, stimulus_buf, CL_TRUE, 0, bytes, stimulus_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Failed to upload stimulus signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, response_buf, CL_TRUE, 0, bytes, response_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Failed to upload response signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, valence_buf, CL_TRUE, 0, bytes, valence_signal, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Failed to upload valence signal: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    err = clEnqueueWriteBuffer(queue, context_vector_buf, CL_TRUE, 0, vector_bytes, zero_vec, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Failed to clear context accumulator: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    count_cl = (cl_int)signal_count;
    recency_bias_cl = (cl_float)recency_bias;
    significance_scale_cl = (cl_float)significance_scale;

    err  = clSetKernelArg(context_resonance_kernel, 0, sizeof(cl_mem), &stimulus_buf);
    err |= clSetKernelArg(context_resonance_kernel, 1, sizeof(cl_mem), &response_buf);
    err |= clSetKernelArg(context_resonance_kernel, 2, sizeof(cl_mem), &valence_buf);
    err |= clSetKernelArg(context_resonance_kernel, 3, sizeof(cl_mem), &context_field_buf);
    err |= clSetKernelArg(context_resonance_kernel, 4, sizeof(cl_mem), &context_vector_buf);
    err |= clSetKernelArg(context_resonance_kernel, 5, sizeof(cl_int), &count_cl);
    err |= clSetKernelArg(context_resonance_kernel, 6, sizeof(cl_float), &recency_bias_cl);
    err |= clSetKernelArg(context_resonance_kernel, 7, sizeof(cl_float), &significance_scale_cl);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    global = (size_t)signal_count;
    err = ENQUEUE_KERNEL_PROFILED(context_resonance_kernel, 1, &global, NULL, "context_resonance");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Kernel launch failed: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }

    err = clEnqueueReadBuffer(queue, context_vector_buf, CL_TRUE, 0, vector_bytes, context_vector_out, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Context Bridge: Failed to read context vector: %s (%d)\n", clGetErrorString(err), err);
        goto cleanup;
    }
    if (context_field_out) {
        err = clEnqueueReadBuffer(queue, context_field_buf, CL_TRUE, 0, bytes, context_field_out, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] Context Bridge: Failed to read context field: %s (%d)\n", clGetErrorString(err), err);
            goto cleanup;
        }
    }
    if (signal_count > 0) {
        for (int i = 0; i < 3; ++i) {
            context_vector_out[i] /= (float)signal_count;
        }
    }

    success = 1;

cleanup:
    if (!success) {
        for (int i = 0; i < 3; ++i) { context_vector_out[i] = 0.0f; }
        if (context_field_out && bytes > 0) { memset(context_field_out, 0, bytes); }
    }
    if (context_vector_buf) { clReleaseMemObject(context_vector_buf); }
    if (context_field_buf) { clReleaseMemObject(context_field_buf); }
    if (valence_buf) { clReleaseMemObject(valence_buf); }
    if (response_buf) { clReleaseMemObject(response_buf); }
    if (stimulus_buf) { clReleaseMemObject(stimulus_buf); }
    return success;
}

DLLEXPORT int generate_dream_state_gpu(int gpu_index, int signal_count,
                                       const float* qualia_vector,
                                       const float* intuition_vector,
                                       const float* context_vector,
                                       const float* gradient_signal,
                                       const float* flux_signal,
                                       const float* field_signal,
                                       const float* behavior_signal,
                                       const float* target_qualia_vector,
                                       float* ideal_gradient_out,
                                       float* ideal_flux_out,
                                       float* ideal_field_out,
                                       float* ideal_behavior_out,
                                       float* latent_vector_out) {
    size_t signal_bytes = (size_t)((signal_count > 0) ? signal_count : 0) * sizeof(float);
    size_t latent_bytes = latent_vector_out ? (size_t)signal_count * 4u * sizeof(float) : 0;
    const size_t vec4_bytes = 4 * sizeof(float);
    const size_t vec3_bytes = 3 * sizeof(float);
    if (signal_count <= 0 || !qualia_vector || !intuition_vector || !context_vector ||
        !gradient_signal || !flux_signal || !field_signal || !behavior_signal ||
        !target_qualia_vector || !ideal_gradient_out || !ideal_flux_out ||
        !ideal_field_out || !ideal_behavior_out) {
        if (ideal_gradient_out && signal_bytes > 0) { memset(ideal_gradient_out, 0, signal_bytes); }
        if (ideal_flux_out && signal_bytes > 0) { memset(ideal_flux_out, 0, signal_bytes); }
        if (ideal_field_out && signal_bytes > 0) { memset(ideal_field_out, 0, signal_bytes); }
        if (ideal_behavior_out && signal_bytes > 0) { memset(ideal_behavior_out, 0, signal_bytes); }
        if (latent_vector_out && latent_bytes > 0) { memset(latent_vector_out, 0, latent_bytes); }
        fprintf(stderr, "[C] Dream Generator: Invalid inputs.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        if (ideal_gradient_out && signal_bytes > 0) { memset(ideal_gradient_out, 0, signal_bytes); }
        if (ideal_flux_out && signal_bytes > 0) { memset(ideal_flux_out, 0, signal_bytes); }
        if (ideal_field_out && signal_bytes > 0) { memset(ideal_field_out, 0, signal_bytes); }
        if (ideal_behavior_out && signal_bytes > 0) { memset(ideal_behavior_out, 0, signal_bytes); }
        if (latent_vector_out && latent_bytes > 0) { memset(latent_vector_out, 0, latent_bytes); }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem qualia_buf = NULL;
    cl_mem intuition_buf = NULL;
    cl_mem context_buf = NULL;
    cl_mem gradient_buf = NULL;
    cl_mem flux_buf = NULL;
    cl_mem field_buf = NULL;
    cl_mem behavior_buf = NULL;
    cl_mem target_buf = NULL;
    cl_mem ideal_grad_buf = NULL;
    cl_mem ideal_flux_buf = NULL;
    cl_mem ideal_field_buf = NULL;
    cl_mem ideal_behavior_buf = NULL;
    cl_mem latent_buf = NULL;
    cl_int count_cl = (cl_int)signal_count;
    size_t global = (size_t)signal_count;
    int success = 0;

    qualia_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec4_bytes, NULL, &err);
    if (err != CL_SUCCESS || !qualia_buf) { fprintf(stderr, "[C] Dream Generator: qualia buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    intuition_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec3_bytes, NULL, &err);
    if (err != CL_SUCCESS || !intuition_buf) { fprintf(stderr, "[C] Dream Generator: intuition buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    context_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec3_bytes, NULL, &err);
    if (err != CL_SUCCESS || !context_buf) { fprintf(stderr, "[C] Dream Generator: context buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    gradient_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !gradient_buf) { fprintf(stderr, "[C] Dream Generator: gradient buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    flux_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !flux_buf) { fprintf(stderr, "[C] Dream Generator: flux buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    field_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !field_buf) { fprintf(stderr, "[C] Dream Generator: field buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    behavior_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !behavior_buf) { fprintf(stderr, "[C] Dream Generator: behavior buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    target_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec4_bytes, NULL, &err);
    if (err != CL_SUCCESS || !target_buf) { fprintf(stderr, "[C] Dream Generator: target buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    ideal_grad_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !ideal_grad_buf) { fprintf(stderr, "[C] Dream Generator: ideal gradient buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    ideal_flux_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !ideal_flux_buf) { fprintf(stderr, "[C] Dream Generator: ideal flux buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    ideal_field_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !ideal_field_buf) { fprintf(stderr, "[C] Dream Generator: ideal field buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    ideal_behavior_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !ideal_behavior_buf) { fprintf(stderr, "[C] Dream Generator: ideal behavior buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    if (latent_vector_out && latent_bytes > 0) {
        latent_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, latent_bytes, NULL, &err);
        if (err != CL_SUCCESS || !latent_buf) { fprintf(stderr, "[C] Dream Generator: latent buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    }

    err = clEnqueueWriteBuffer(queue, qualia_buf, CL_TRUE, 0, vec4_bytes, qualia_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, intuition_buf, CL_TRUE, 0, vec3_bytes, intuition_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, context_buf, CL_TRUE, 0, vec3_bytes, context_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, gradient_buf, CL_TRUE, 0, signal_bytes, gradient_signal, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, flux_buf, CL_TRUE, 0, signal_bytes, flux_signal, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, field_buf, CL_TRUE, 0, signal_bytes, field_signal, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, behavior_buf, CL_TRUE, 0, signal_bytes, behavior_signal, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, target_buf, CL_TRUE, 0, vec4_bytes, target_qualia_vector, 0, NULL, NULL);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Dream Generator: Failed to upload inputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err  = clSetKernelArg(dream_state_generator_kernel, 0, sizeof(cl_mem), &qualia_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 1, sizeof(cl_mem), &intuition_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 2, sizeof(cl_mem), &context_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 3, sizeof(cl_mem), &gradient_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 4, sizeof(cl_mem), &flux_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 5, sizeof(cl_mem), &field_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 6, sizeof(cl_mem), &behavior_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 7, sizeof(cl_mem), &target_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 8, sizeof(cl_mem), &ideal_grad_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 9, sizeof(cl_mem), &ideal_flux_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 10, sizeof(cl_mem), &ideal_field_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 11, sizeof(cl_mem), &ideal_behavior_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 12, sizeof(cl_mem), &latent_buf);
    err |= clSetKernelArg(dream_state_generator_kernel, 13, sizeof(cl_int), &count_cl);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Dream Generator: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    err = ENQUEUE_KERNEL_PROFILED(dream_state_generator_kernel, 1, &global, NULL, "dream_state_generator");
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Dream Generator: Kernel launch failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err = clEnqueueReadBuffer(queue, ideal_grad_buf, CL_TRUE, 0, signal_bytes, ideal_gradient_out, 0, NULL, NULL);
    err |= clEnqueueReadBuffer(queue, ideal_flux_buf, CL_TRUE, 0, signal_bytes, ideal_flux_out, 0, NULL, NULL);
    err |= clEnqueueReadBuffer(queue, ideal_field_buf, CL_TRUE, 0, signal_bytes, ideal_field_out, 0, NULL, NULL);
    err |= clEnqueueReadBuffer(queue, ideal_behavior_buf, CL_TRUE, 0, signal_bytes, ideal_behavior_out, 0, NULL, NULL);
    if (latent_buf && latent_vector_out && latent_bytes > 0) {
        err |= clEnqueueReadBuffer(queue, latent_buf, CL_TRUE, 0, latent_bytes, latent_vector_out, 0, NULL, NULL);
    } else if (latent_vector_out && latent_bytes > 0) {
        memset(latent_vector_out, 0, latent_bytes);
    }
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Dream Generator: Failed to read outputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    success = 1;

cleanup:
    if (!success) {
        if (ideal_gradient_out && signal_bytes > 0) { memset(ideal_gradient_out, 0, signal_bytes); }
        if (ideal_flux_out && signal_bytes > 0) { memset(ideal_flux_out, 0, signal_bytes); }
        if (ideal_field_out && signal_bytes > 0) { memset(ideal_field_out, 0, signal_bytes); }
        if (ideal_behavior_out && signal_bytes > 0) { memset(ideal_behavior_out, 0, signal_bytes); }
        if (latent_vector_out && latent_bytes > 0) { memset(latent_vector_out, 0, latent_bytes); }
    }
    if (latent_buf) { clReleaseMemObject(latent_buf); }
    if (ideal_behavior_buf) { clReleaseMemObject(ideal_behavior_buf); }
    if (ideal_field_buf) { clReleaseMemObject(ideal_field_buf); }
    if (ideal_flux_buf) { clReleaseMemObject(ideal_flux_buf); }
    if (ideal_grad_buf) { clReleaseMemObject(ideal_grad_buf); }
    if (target_buf) { clReleaseMemObject(target_buf); }
    if (behavior_buf) { clReleaseMemObject(behavior_buf); }
    if (field_buf) { clReleaseMemObject(field_buf); }
    if (flux_buf) { clReleaseMemObject(flux_buf); }
    if (gradient_buf) { clReleaseMemObject(gradient_buf); }
    if (context_buf) { clReleaseMemObject(context_buf); }
    if (intuition_buf) { clReleaseMemObject(intuition_buf); }
    if (qualia_buf) { clReleaseMemObject(qualia_buf); }
    return success;
}

DLLEXPORT int plan_transformation_gpu(int gpu_index, int signal_count,
                                      const float* current_gradient,
                                      const float* current_flux,
                                      const float* dream_gradient,
                                      const float* dream_flux,
                                      const float* qualia_vector,
                                      const float* context_vector,
                                      float learning_rate,
                                      float exploration_bias,
                                      float* plan_scores_out,
                                      float* plan_matrix_out) {
    size_t signal_bytes = (size_t)((signal_count > 0) ? signal_count : 0) * sizeof(float);
    size_t matrix_bytes = plan_matrix_out ? (size_t)signal_count * 4u * sizeof(float) : 0;
    const size_t vec4_bytes = 4 * sizeof(float);
    const size_t vec3_bytes = 3 * sizeof(float);
    if (signal_count <= 0 || !current_gradient || !current_flux || !dream_gradient || !dream_flux ||
        !qualia_vector || !context_vector || !plan_scores_out || !plan_matrix_out) {
        if (plan_scores_out && signal_bytes > 0) { memset(plan_scores_out, 0, signal_bytes); }
        if (plan_matrix_out && matrix_bytes > 0) { memset(plan_matrix_out, 0, matrix_bytes); }
        fprintf(stderr, "[C] Transformation Planner: Invalid inputs.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        if (plan_scores_out && signal_bytes > 0) { memset(plan_scores_out, 0, signal_bytes); }
        if (plan_matrix_out && matrix_bytes > 0) { memset(plan_matrix_out, 0, matrix_bytes); }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem current_grad_buf = NULL;
    cl_mem current_flux_buf = NULL;
    cl_mem dream_grad_buf = NULL;
    cl_mem dream_flux_buf = NULL;
    cl_mem qualia_buf = NULL;
    cl_mem context_buf = NULL;
    cl_mem plan_matrix_buf = NULL;
    cl_mem plan_scores_buf = NULL;
    cl_int count_cl = (cl_int)signal_count;
    cl_float lr_cl = (cl_float)learning_rate;
    cl_float exploration_cl = (cl_float)exploration_bias;
    size_t global = (size_t)signal_count;
    int success = 0;

    current_grad_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !current_grad_buf) { fprintf(stderr, "[C] Transformation Planner: current gradient buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    current_flux_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !current_flux_buf) { fprintf(stderr, "[C] Transformation Planner: current flux buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    dream_grad_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !dream_grad_buf) { fprintf(stderr, "[C] Transformation Planner: dream gradient buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    dream_flux_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !dream_flux_buf) { fprintf(stderr, "[C] Transformation Planner: dream flux buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    qualia_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec4_bytes, NULL, &err);
    if (err != CL_SUCCESS || !qualia_buf) { fprintf(stderr, "[C] Transformation Planner: qualia buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    context_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec3_bytes, NULL, &err);
    if (err != CL_SUCCESS || !context_buf) { fprintf(stderr, "[C] Transformation Planner: context buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    plan_matrix_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, matrix_bytes, NULL, &err);
    if (err != CL_SUCCESS || !plan_matrix_buf) { fprintf(stderr, "[C] Transformation Planner: plan matrix buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    plan_scores_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !plan_scores_buf) { fprintf(stderr, "[C] Transformation Planner: plan score buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err = clEnqueueWriteBuffer(queue, current_grad_buf, CL_TRUE, 0, signal_bytes, current_gradient, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, current_flux_buf, CL_TRUE, 0, signal_bytes, current_flux, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, dream_grad_buf, CL_TRUE, 0, signal_bytes, dream_gradient, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, dream_flux_buf, CL_TRUE, 0, signal_bytes, dream_flux, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, qualia_buf, CL_TRUE, 0, vec4_bytes, qualia_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, context_buf, CL_TRUE, 0, vec3_bytes, context_vector, 0, NULL, NULL);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Transformation Planner: Failed to upload inputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err  = clSetKernelArg(transformation_planner_kernel, 0, sizeof(cl_mem), &current_grad_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 1, sizeof(cl_mem), &current_flux_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 2, sizeof(cl_mem), &dream_grad_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 3, sizeof(cl_mem), &dream_flux_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 4, sizeof(cl_mem), &qualia_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 5, sizeof(cl_mem), &context_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 6, sizeof(cl_mem), &plan_matrix_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 7, sizeof(cl_mem), &plan_scores_buf);
    err |= clSetKernelArg(transformation_planner_kernel, 8, sizeof(cl_int), &count_cl);
    err |= clSetKernelArg(transformation_planner_kernel, 9, sizeof(cl_float), &lr_cl);
    err |= clSetKernelArg(transformation_planner_kernel, 10, sizeof(cl_float), &exploration_cl);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Transformation Planner: Failed to set kernel args: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    err = ENQUEUE_KERNEL_PROFILED(transformation_planner_kernel, 1, &global, NULL, "transformation_planner");
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Transformation Planner: Kernel launch failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err = clEnqueueReadBuffer(queue, plan_scores_buf, CL_TRUE, 0, signal_bytes, plan_scores_out, 0, NULL, NULL);
    err |= clEnqueueReadBuffer(queue, plan_matrix_buf, CL_TRUE, 0, matrix_bytes, plan_matrix_out, 0, NULL, NULL);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Transformation Planner: Failed to read outputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    success = 1;

cleanup:
    if (!success) {
        if (plan_scores_out && signal_bytes > 0) { memset(plan_scores_out, 0, signal_bytes); }
        if (plan_matrix_out && matrix_bytes > 0) { memset(plan_matrix_out, 0, matrix_bytes); }
    }
    if (plan_scores_buf) { clReleaseMemObject(plan_scores_buf); }
    if (plan_matrix_buf) { clReleaseMemObject(plan_matrix_buf); }
    if (context_buf) { clReleaseMemObject(context_buf); }
    if (qualia_buf) { clReleaseMemObject(qualia_buf); }
    if (dream_flux_buf) { clReleaseMemObject(dream_flux_buf); }
    if (dream_grad_buf) { clReleaseMemObject(dream_grad_buf); }
    if (current_flux_buf) { clReleaseMemObject(current_flux_buf); }
    if (current_grad_buf) { clReleaseMemObject(current_grad_buf); }
    return success;
}

DLLEXPORT int generate_system_narrative_gpu(int gpu_index, int signal_count,
                                            const float* qualia_vector,
                                            const float* intuition_vector,
                                            const float* context_vector,
                                            const float* dream_latent,
                                            int latent_stride,
                                            const float* plan_matrix,
                                            int plan_stride,
                                            const float* plan_scores,
                                            float* narrative_embeddings_out,
                                            float* narrative_weights_out,
                                            float* narrative_summary_out) {
    const int embed_stride = 4;
    const size_t signal_bytes = (signal_count > 0 ? (size_t)signal_count : 0u) * sizeof(float);
    const size_t latent_bytes = (dream_latent && latent_stride > 0 && signal_count > 0)
                                ? (size_t)signal_count * (size_t)latent_stride * sizeof(float) : 0;
    const size_t plan_bytes = (plan_matrix && plan_stride > 0 && signal_count > 0)
                              ? (size_t)signal_count * (size_t)plan_stride * sizeof(float) : 0;
    const size_t embed_bytes = (narrative_embeddings_out && signal_count > 0)
                               ? (size_t)signal_count * (size_t)embed_stride * sizeof(float) : 0;
    const size_t summary_bytes = narrative_summary_out ? 4u * sizeof(float) : 0u;
    if (signal_count <= 0 || !qualia_vector || !intuition_vector || !context_vector ||
        !dream_latent || latent_stride <= 0 || !plan_matrix || plan_stride <= 0 ||
        !plan_scores || !narrative_embeddings_out || !narrative_weights_out) {
        if (narrative_embeddings_out && embed_bytes > 0) { memset(narrative_embeddings_out, 0, embed_bytes); }
        if (narrative_weights_out && signal_bytes > 0) { memset(narrative_weights_out, 0, signal_bytes); }
        if (narrative_summary_out && summary_bytes > 0) { memset(narrative_summary_out, 0, summary_bytes); }
        fprintf(stderr, "[C] Narrative Kernel: Invalid inputs.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        if (narrative_embeddings_out && embed_bytes > 0) { memset(narrative_embeddings_out, 0, embed_bytes); }
        if (narrative_weights_out && signal_bytes > 0) { memset(narrative_weights_out, 0, signal_bytes); }
        if (narrative_summary_out && summary_bytes > 0) { memset(narrative_summary_out, 0, summary_bytes); }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem qualia_buf = NULL;
    cl_mem intuition_buf = NULL;
    cl_mem context_buf = NULL;
    cl_mem latent_buf = NULL;
    cl_mem plan_buf = NULL;
    cl_mem score_buf = NULL;
    cl_mem embed_buf = NULL;
    cl_mem weight_buf = NULL;
    cl_mem summary_buf = NULL;
    cl_int count_cl = (cl_int)signal_count;
    cl_int latent_stride_cl = (cl_int)latent_stride;
    cl_int plan_stride_cl = (cl_int)plan_stride;
    size_t global = (size_t)signal_count;
    int success = 0;

    const size_t vec4_bytes = 4 * sizeof(float);
    const size_t vec3_bytes = 3 * sizeof(float);

    qualia_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec4_bytes, NULL, &err);
    if (err != CL_SUCCESS || !qualia_buf) { fprintf(stderr, "[C] Narrative Kernel: qualia buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    intuition_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec3_bytes, NULL, &err);
    if (err != CL_SUCCESS || !intuition_buf) { fprintf(stderr, "[C] Narrative Kernel: intuition buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    context_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, vec3_bytes, NULL, &err);
    if (err != CL_SUCCESS || !context_buf) { fprintf(stderr, "[C] Narrative Kernel: context buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    latent_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, latent_bytes, NULL, &err);
    if (err != CL_SUCCESS || !latent_buf) { fprintf(stderr, "[C] Narrative Kernel: latent buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    plan_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, plan_bytes, NULL, &err);
    if (err != CL_SUCCESS || !plan_buf) { fprintf(stderr, "[C] Narrative Kernel: plan buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    score_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !score_buf) { fprintf(stderr, "[C] Narrative Kernel: score buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    embed_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, embed_bytes, NULL, &err);
    if (err != CL_SUCCESS || !embed_buf) { fprintf(stderr, "[C] Narrative Kernel: embed buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    weight_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !weight_buf) { fprintf(stderr, "[C] Narrative Kernel: weight buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    if (narrative_summary_out) {
        summary_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, summary_bytes, NULL, &err);
        if (err != CL_SUCCESS || !summary_buf) { fprintf(stderr, "[C] Narrative Kernel: summary buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    }

    err  = clEnqueueWriteBuffer(queue, qualia_buf, CL_TRUE, 0, vec4_bytes, qualia_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, intuition_buf, CL_TRUE, 0, vec3_bytes, intuition_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, context_buf, CL_TRUE, 0, vec3_bytes, context_vector, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, latent_buf, CL_TRUE, 0, latent_bytes, dream_latent, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, plan_buf, CL_TRUE, 0, plan_bytes, plan_matrix, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, score_buf, CL_TRUE, 0, signal_bytes, plan_scores, 0, NULL, NULL);
    if (summary_buf) {
        const float zero_summary[4] = {0.0f, 0.0f, 0.0f, 0.0f};
        err |= clEnqueueWriteBuffer(queue, summary_buf, CL_TRUE, 0, summary_bytes, zero_summary, 0, NULL, NULL);
    }
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Narrative Kernel: Failed to upload inputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err  = clSetKernelArg(system_narrative_kernel, 0, sizeof(cl_mem), &qualia_buf);
    err |= clSetKernelArg(system_narrative_kernel, 1, sizeof(cl_mem), &intuition_buf);
    err |= clSetKernelArg(system_narrative_kernel, 2, sizeof(cl_mem), &context_buf);
    err |= clSetKernelArg(system_narrative_kernel, 3, sizeof(cl_mem), &latent_buf);
    err |= clSetKernelArg(system_narrative_kernel, 4, sizeof(cl_mem), &plan_buf);
    err |= clSetKernelArg(system_narrative_kernel, 5, sizeof(cl_mem), &score_buf);
    err |= clSetKernelArg(system_narrative_kernel, 6, sizeof(cl_mem), &embed_buf);
    err |= clSetKernelArg(system_narrative_kernel, 7, sizeof(cl_mem), &weight_buf);
    err |= clSetKernelArg(system_narrative_kernel, 8, sizeof(cl_mem), &summary_buf);
    err |= clSetKernelArg(system_narrative_kernel, 9, sizeof(cl_int), &count_cl);
    err |= clSetKernelArg(system_narrative_kernel, 10, sizeof(cl_int), &latent_stride_cl);
    err |= clSetKernelArg(system_narrative_kernel, 11, sizeof(cl_int), &plan_stride_cl);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Narrative Kernel: Failed to set args: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    err = ENQUEUE_KERNEL_PROFILED(system_narrative_kernel, 1, &global, NULL, "system_narrative");
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Narrative Kernel: launch failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err = clEnqueueReadBuffer(queue, embed_buf, CL_TRUE, 0, embed_bytes, narrative_embeddings_out, 0, NULL, NULL);
    err |= clEnqueueReadBuffer(queue, weight_buf, CL_TRUE, 0, signal_bytes, narrative_weights_out, 0, NULL, NULL);
    if (summary_buf && narrative_summary_out) {
        err |= clEnqueueReadBuffer(queue, summary_buf, CL_TRUE, 0, summary_bytes, narrative_summary_out, 0, NULL, NULL);
        if (err == CL_SUCCESS && signal_count > 0) {
            float inv = 1.0f / (float)signal_count;
            for (int i = 0; i < 4; ++i) {
                narrative_summary_out[i] *= inv;
            }
        }
    }
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Narrative Kernel: Failed to read outputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    success = 1;

cleanup:
    if (!success) {
        if (narrative_embeddings_out && embed_bytes > 0) { memset(narrative_embeddings_out, 0, embed_bytes); }
        if (narrative_weights_out && signal_bytes > 0) { memset(narrative_weights_out, 0, signal_bytes); }
        if (narrative_summary_out && summary_bytes > 0) { memset(narrative_summary_out, 0, summary_bytes); }
    }
    if (summary_buf) { clReleaseMemObject(summary_buf); }
    if (weight_buf) { clReleaseMemObject(weight_buf); }
    if (embed_buf) { clReleaseMemObject(embed_buf); }
    if (score_buf) { clReleaseMemObject(score_buf); }
    if (plan_buf) { clReleaseMemObject(plan_buf); }
    if (latent_buf) { clReleaseMemObject(latent_buf); }
    if (context_buf) { clReleaseMemObject(context_buf); }
    if (intuition_buf) { clReleaseMemObject(intuition_buf); }
    if (qualia_buf) { clReleaseMemObject(qualia_buf); }
    return success;
}

DLLEXPORT int abstract_to_symbolic_concepts_gpu(int gpu_index, int signal_count,
                                                const float* narrative_embeddings,
                                                const float* narrative_weights,
                                                int embedding_stride,
                                                float* concept_codes_out,
                                                float* concept_strength_out,
                                                float* concept_summary_out) {
    const size_t signal_bytes = (signal_count > 0 ? (size_t)signal_count : 0u) * sizeof(float);
    const size_t embed_bytes = (narrative_embeddings && embedding_stride > 0 && signal_count > 0)
                               ? (size_t)signal_count * (size_t)embedding_stride * sizeof(float) : 0;
    const size_t concept_bytes = (concept_codes_out && signal_count > 0) ? signal_bytes : 0;
    const size_t summary_bytes = concept_summary_out ? 4u * sizeof(float) : 0u;
    if (signal_count <= 0 || !narrative_embeddings || embedding_stride <= 0 ||
        !narrative_weights || !concept_codes_out || !concept_strength_out) {
        if (concept_codes_out && concept_bytes > 0) { memset(concept_codes_out, 0, concept_bytes); }
        if (concept_strength_out && concept_bytes > 0) { memset(concept_strength_out, 0, concept_bytes); }
        if (concept_summary_out && summary_bytes > 0) { memset(concept_summary_out, 0, summary_bytes); }
        fprintf(stderr, "[C] Symbolic Kernel: Invalid inputs.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        if (concept_codes_out && concept_bytes > 0) { memset(concept_codes_out, 0, concept_bytes); }
        if (concept_strength_out && concept_bytes > 0) { memset(concept_strength_out, 0, concept_bytes); }
        if (concept_summary_out && summary_bytes > 0) { memset(concept_summary_out, 0, summary_bytes); }
        return 0;
    }

    cl_int err = CL_SUCCESS;
    cl_mem embed_buf = NULL;
    cl_mem weight_buf = NULL;
    cl_mem code_buf = NULL;
    cl_mem strength_buf = NULL;
    cl_mem summary_buf = NULL;
    cl_int count_cl = (cl_int)signal_count;
    cl_int stride_cl = (cl_int)embedding_stride;
    size_t global = (size_t)signal_count;
    int success = 0;

    embed_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, embed_bytes, NULL, &err);
    if (err != CL_SUCCESS || !embed_buf) { fprintf(stderr, "[C] Symbolic Kernel: embed buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    weight_buf = clCreateBuffer(context, CL_MEM_READ_ONLY, signal_bytes, NULL, &err);
    if (err != CL_SUCCESS || !weight_buf) { fprintf(stderr, "[C] Symbolic Kernel: weight buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    code_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, concept_bytes, NULL, &err);
    if (err != CL_SUCCESS || !code_buf) { fprintf(stderr, "[C] Symbolic Kernel: code buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    strength_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, concept_bytes, NULL, &err);
    if (err != CL_SUCCESS || !strength_buf) { fprintf(stderr, "[C] Symbolic Kernel: strength buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    if (concept_summary_out) {
        summary_buf = clCreateBuffer(context, CL_MEM_READ_WRITE, summary_bytes, NULL, &err);
        if (err != CL_SUCCESS || !summary_buf) { fprintf(stderr, "[C] Symbolic Kernel: summary buffer alloc failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }
    }

    err  = clEnqueueWriteBuffer(queue, embed_buf, CL_TRUE, 0, embed_bytes, narrative_embeddings, 0, NULL, NULL);
    err |= clEnqueueWriteBuffer(queue, weight_buf, CL_TRUE, 0, signal_bytes, narrative_weights, 0, NULL, NULL);
    if (summary_buf) {
        const float zero_summary[4] = {0.0f, 0.0f, 0.0f, 0.0f};
        err |= clEnqueueWriteBuffer(queue, summary_buf, CL_TRUE, 0, summary_bytes, zero_summary, 0, NULL, NULL);
    }
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Symbolic Kernel: Failed to upload inputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err  = clSetKernelArg(symbolic_abstraction_kernel, 0, sizeof(cl_mem), &embed_buf);
    err |= clSetKernelArg(symbolic_abstraction_kernel, 1, sizeof(cl_mem), &weight_buf);
    err |= clSetKernelArg(symbolic_abstraction_kernel, 2, sizeof(cl_mem), &code_buf);
    err |= clSetKernelArg(symbolic_abstraction_kernel, 3, sizeof(cl_mem), &strength_buf);
    err |= clSetKernelArg(symbolic_abstraction_kernel, 4, sizeof(cl_mem), &summary_buf);
    err |= clSetKernelArg(symbolic_abstraction_kernel, 5, sizeof(cl_int), &count_cl);
    err |= clSetKernelArg(symbolic_abstraction_kernel, 6, sizeof(cl_int), &stride_cl);
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Symbolic Kernel: Failed to set args: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    g_thread_queue = queue;
    g_thread_gpu_index = gpu_index;
    err = ENQUEUE_KERNEL_PROFILED(symbolic_abstraction_kernel, 1, &global, NULL, "symbolic_abstraction");
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Symbolic Kernel: launch failed: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    err = clEnqueueReadBuffer(queue, code_buf, CL_TRUE, 0, concept_bytes, concept_codes_out, 0, NULL, NULL);
    err |= clEnqueueReadBuffer(queue, strength_buf, CL_TRUE, 0, concept_bytes, concept_strength_out, 0, NULL, NULL);
    if (summary_buf && concept_summary_out) {
        err |= clEnqueueReadBuffer(queue, summary_buf, CL_TRUE, 0, summary_bytes, concept_summary_out, 0, NULL, NULL);
        if (err == CL_SUCCESS && signal_count > 0) {
            float inv = 1.0f / (float)signal_count;
            for (int i = 0; i < 4; ++i) {
                concept_summary_out[i] *= inv;
            }
        }
    }
    if (err != CL_SUCCESS) { fprintf(stderr, "[C] Symbolic Kernel: Failed to read outputs: %s (%d)\n", clGetErrorString(err), err); goto cleanup; }

    success = 1;

cleanup:
    if (!success) {
        if (concept_codes_out && concept_bytes > 0) { memset(concept_codes_out, 0, concept_bytes); }
        if (concept_strength_out && concept_bytes > 0) { memset(concept_strength_out, 0, concept_bytes); }
        if (concept_summary_out && summary_bytes > 0) { memset(concept_summary_out, 0, summary_bytes); }
    }
    if (summary_buf) { clReleaseMemObject(summary_buf); }
    if (strength_buf) { clReleaseMemObject(strength_buf); }
    if (code_buf) { clReleaseMemObject(code_buf); }
    if (weight_buf) { clReleaseMemObject(weight_buf); }
    if (embed_buf) { clReleaseMemObject(embed_buf); }
    return success;
}

DLLEXPORT int execute_qaoa_gpu(int gpu_index, int num_qubits, int p_layers,
                               const float* gammas, const float* betas, int num_parameters,
                               const PauliZTerm* cost_terms, int num_cost_terms,
                               float* out_energy) {
    (void)gpu_index;
    if (num_qubits <= 0 || p_layers <= 0 || !gammas || !betas || !cost_terms || num_cost_terms <= 0) {
        fprintf(stderr, "[C] QAOA: Invalid configuration.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (num_parameters < p_layers) {
        fprintf(stderr, "[C] QAOA: Parameter arrays shorter than layer count (%d < %d).\n", num_parameters, p_layers);
        return 0;
    }

    QuantumStateGPU state = {0};
    if (!quantum_allocate_state(num_qubits, &state)) { return 0; }

    int success = 0;
    if (!quantum_prepare_uniform_superposition(&state, num_qubits, 0)) { goto cleanup; }

    for (int layer = 0; layer < p_layers; ++layer) {
        float gamma = gammas[layer];
        float beta = betas[layer];
        for (int t = 0; t < num_cost_terms; ++t) {
            float angle = -gamma * cost_terms[t].coefficient;
            if (!quantum_apply_multi_qubit_z_phase(&state, cost_terms[t].z_mask, angle)) { goto cleanup; }
        }
        for (int q = 0; q < num_qubits; ++q) {
            if (!quantum_apply_rotation_x(&state, q, 2.0f * beta)) { goto cleanup; }
        }
    }

    if (out_energy) {
        float energy = 0.0f;
        if (!quantum_compute_pauli_z_energy(&state, cost_terms, num_cost_terms, &energy)) { goto cleanup; }
        *out_energy = energy;
    }

    success = 1;

cleanup:
    quantum_release_state(&state);
    return success;
}

DLLEXPORT int execute_hhl_gpu(int gpu_index, const float* matrix_A, const float* vector_b,
                              int system_size, float* out_solution, int solution_length) {
    (void)gpu_index;
    if (!matrix_A || !vector_b || system_size <= 0) {
        fprintf(stderr, "[C] HHL: Invalid inputs.\n");
        return 0;
    }
    if (out_solution && solution_length < system_size) {
        fprintf(stderr, "[C] HHL: Solution buffer too small (have %d need %d).\n", solution_length, system_size);
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }

    uint32_t rounded = round_up_to_power_of_two((uint32_t)system_size);
    if (rounded != (uint32_t)system_size) {
        fprintf(stderr, "[C] HHL: System size must be a power of two (got %d).\n", system_size);
        return 0;
    }
    int num_system_qubits = 0;
    while ((1 << num_system_qubits) < system_size) { ++num_system_qubits; }

    float* solution = (float*)malloc(system_size * sizeof(float));
    if (!solution) {
        fprintf(stderr, "[C] HHL: Failed to allocate solution workspace.\n");
        return 0;
    }
    if (!solve_linear_system(matrix_A, vector_b, system_size, solution)) {
        fprintf(stderr, "[C] HHL: Linear system solver failed (matrix may be singular).\n");
        free(solution);
        return 0;
    }

    QuantumStateGPU state = {0};
    if (!quantum_allocate_state(num_system_qubits, &state)) {
        free(solution);
        return 0;
    }

    size_t dimension = state.dimension;
    cl_float2* amplitudes = (cl_float2*)calloc(dimension, sizeof(cl_float2));
    if (!amplitudes) {
        fprintf(stderr, "[C] HHL: Failed to allocate amplitude buffer.\n");
        quantum_release_state(&state);
        free(solution);
        return 0;
    }
    double norm = 0.0;
    for (int i = 0; i < system_size; ++i) {
        norm += (double)solution[i] * (double)solution[i];
    }
    if (norm <= 0.0) {
        fprintf(stderr, "[C] HHL: Solution norm is zero.\n");
        free(amplitudes);
        quantum_release_state(&state);
        free(solution);
        return 0;
    }
    double inv_norm = 1.0 / sqrt(norm);
    for (int i = 0; i < system_size; ++i) {
        amplitudes[i].s[0] = (float)(solution[i] * inv_norm);
        amplitudes[i].s[1] = 0.0f;
    }

    if (!quantum_initialize_zero_state(&state)) {
        free(amplitudes);
        quantum_release_state(&state);
        free(solution);
        return 0;
    }
    cl_int err = clEnqueueWriteBuffer(queue, state.buffer, CL_TRUE, 0, dimension * sizeof(cl_float2), amplitudes, 0, NULL, NULL);
    free(amplitudes);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] HHL: Failed to upload solution amplitudes: %s (%d)\n", clGetErrorString(err), err);
        quantum_release_state(&state);
        free(solution);
        return 0;
    }

    if (out_solution) {
        memcpy(out_solution, solution, system_size * sizeof(float));
    }

    free(solution);
    quantum_release_state(&state);
    return 1;
}

DLLEXPORT int execute_qml_classifier_gpu(int gpu_index, int num_qubits,
                                         const float* feature_vector, int num_features,
                                         const float* parameters, int num_parameters,
                                         float* out_expectations, int expectation_length) {
    (void)gpu_index;
    if (num_qubits <= 0 || !feature_vector || num_features <= 0 || !parameters || num_parameters < num_qubits) {
        fprintf(stderr, "[C] QML: Invalid configuration.\n");
        return 0;
    }
    if (out_expectations && expectation_length < num_qubits) {
        fprintf(stderr, "[C] QML: Expectation buffer too small (have %d need %d).\n", expectation_length, num_qubits);
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) { return 0; }

    QuantumStateGPU state = {0};
    if (!quantum_allocate_state(num_qubits, &state)) { return 0; }

    int success = 0;
    if (!quantum_prepare_feature_map(&state, feature_vector, num_features)) { goto cleanup; }
    if (!quantum_apply_qml_classifier_layer(&state, parameters, num_qubits)) { goto cleanup; }

    if (out_expectations) {
        for (int q = 0; q < num_qubits; ++q) {
            float expectation = 0.0f;
            if (!quantum_expectation_pauli_z_gpu(&state, (uint64_t)1 << q, &expectation)) { goto cleanup; }
            out_expectations[q] = expectation;
        }
    }

    success = 1;

cleanup:
    quantum_release_state(&state);
    return success;
}

DLLEXPORT int execute_qec_cycle_gpu(int gpu_index, int code_type, uint32_t error_mask,
                                    float* out_syndrome, int syndrome_length) {
    (void)gpu_index;
    if (!out_syndrome) { fprintf(stderr, "[C] QEC: Syndrome output buffer is NULL.\n"); return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }

    int num_qubits = 0;
    int required_syndromes = 0;
    switch (code_type) {
        case 0: // 3-qubit bit-flip code
        case 1: // 3-qubit phase-flip code
            num_qubits = 3;
            required_syndromes = 2;
            break;
        case 2: // [[7,1,3]] Steane code
            num_qubits = 7;
            required_syndromes = 6;
            break;
        default:
            fprintf(stderr, "[C] QEC: Unsupported code type %d.\n", code_type);
            return 0;
    }

    if (syndrome_length < required_syndromes) {
        fprintf(stderr, "[C] QEC: Syndrome buffer too small (have %d need %d).\n", syndrome_length, required_syndromes);
        return 0;
    }

    QuantumStateGPU state = {0};
    if (!quantum_allocate_state(num_qubits, &state)) { return 0; }

    int success = 0;
    if (code_type == 0) {
        if (!quantum_initialize_zero_state(&state)) { goto cleanup; }
        for (int q = 0; q < num_qubits; ++q) {
            if (error_mask & (1u << q)) {
                if (!quantum_apply_pauli_x(&state, q)) { goto cleanup; }
            }
        }
        float parity12 = 0.0f;
        float parity23 = 0.0f;
        if (!quantum_expectation_pauli_z_gpu(&state, ((uint64_t)1 << 0) | ((uint64_t)1 << 1), &parity12)) { goto cleanup; }
        if (!quantum_expectation_pauli_z_gpu(&state, ((uint64_t)1 << 1) | ((uint64_t)1 << 2), &parity23)) { goto cleanup; }
        float synd0 = 0.5f * (1.0f - parity12);
        float synd1 = 0.5f * (1.0f - parity23);
        if (synd0 < 0.0f) synd0 = 0.0f; else if (synd0 > 1.0f) synd0 = 1.0f;
        if (synd1 < 0.0f) synd1 = 0.0f; else if (synd1 > 1.0f) synd1 = 1.0f;
        out_syndrome[0] = synd0;
        out_syndrome[1] = synd1;
    } else if (code_type == 1) {
        if (!quantum_initialize_zero_state(&state)) { goto cleanup; }
        for (int q = 0; q < num_qubits; ++q) {
            if (!quantum_apply_hadamard(&state, q)) { goto cleanup; }
        }
        for (int q = 0; q < num_qubits; ++q) {
            if (error_mask & (1u << q)) {
                if (!quantum_apply_pauli_z(&state, q)) { goto cleanup; }
            }
        }
        for (int q = 0; q < num_qubits; ++q) {
            if (!quantum_apply_hadamard(&state, q)) { goto cleanup; }
        }
        float parity12 = 0.0f;
        float parity23 = 0.0f;
        if (!quantum_expectation_pauli_z_gpu(&state, ((uint64_t)1 << 0) | ((uint64_t)1 << 1), &parity12)) { goto cleanup; }
        if (!quantum_expectation_pauli_z_gpu(&state, ((uint64_t)1 << 1) | ((uint64_t)1 << 2), &parity23)) { goto cleanup; }
        float synd0 = 0.5f * (1.0f - parity12);
        float synd1 = 0.5f * (1.0f - parity23);
        if (synd0 < 0.0f) synd0 = 0.0f; else if (synd0 > 1.0f) synd0 = 1.0f;
        if (synd1 < 0.0f) synd1 = 0.0f; else if (synd1 > 1.0f) synd1 = 1.0f;
        out_syndrome[0] = synd0;
        out_syndrome[1] = synd1;
    } else if (code_type == 2) {
        if (!quantum_prepare_steane_zero_state(&state)) { goto cleanup; }
        uint32_t x_mask = error_mask & 0x7Fu;
        uint32_t z_mask = (error_mask >> 7) & 0x7Fu;
        uint32_t y_mask = (error_mask >> 14) & 0x7Fu;
        for (int q = 0; q < num_qubits; ++q) {
            uint32_t bit = (uint32_t)1u << q;
            if (y_mask & bit) {
                if (!quantum_apply_pauli_z(&state, q)) { goto cleanup; }
                if (!quantum_apply_pauli_x(&state, q)) { goto cleanup; }
                continue;
            }
            if (x_mask & bit) {
                if (!quantum_apply_pauli_x(&state, q)) { goto cleanup; }
            }
            if (z_mask & bit) {
                if (!quantum_apply_pauli_z(&state, q)) { goto cleanup; }
            }
        }
        static const int steane_stabilizers[3][4] = {
            {0, 1, 2, 4},
            {0, 2, 3, 5},
            {1, 2, 3, 6}
        };
        for (int s = 0; s < 3; ++s) {
            uint64_t z_mask_check = 0;
            for (int j = 0; j < 4; ++j) {
                z_mask_check |= ((uint64_t)1 << steane_stabilizers[s][j]);
            }
            float expectation = 0.0f;
            if (!quantum_expectation_pauli_z_gpu(&state, z_mask_check, &expectation)) { goto cleanup; }
            float syndrome = 0.5f * (1.0f - expectation);
            if (syndrome < 0.0f) syndrome = 0.0f; else if (syndrome > 1.0f) syndrome = 1.0f;
            out_syndrome[s] = syndrome;
        }
        for (int s = 0; s < 3; ++s) {
            float expectation = 0.0f;
            if (!quantum_measure_x_parity_gpu(&state, steane_stabilizers[s], 4, &expectation)) { goto cleanup; }
            float syndrome = 0.5f * (1.0f - expectation);
            if (syndrome < 0.0f) syndrome = 0.0f; else if (syndrome > 1.0f) syndrome = 1.0f;
            out_syndrome[3 + s] = syndrome;
        }
    }

    success = 1;

cleanup:
    quantum_release_state(&state);
    return success;
}

DLLEXPORT int quantum_upload_gate_sequence(int gpu_index, const QuantumGate* gates, int gate_count) {
    (void)gpu_index;
    if (gate_count <= 0 || !gates) {
        fprintf(stderr, "[C] Quantum: Invalid gate sequence upload (count=%d, ptr=%p).\n", gate_count, (const void*)gates);
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        return 0;
    }

    size_t bytes = (size_t)gate_count * sizeof(QuantumGate);
    if (quantum_gate_host_sequence) {
        free(quantum_gate_host_sequence);
        quantum_gate_host_sequence = NULL;
    }
    if (quantum_gate_sequence_buffer) {
        clReleaseMemObject(quantum_gate_sequence_buffer);
        quantum_gate_sequence_buffer = NULL;
    }

    quantum_gate_host_sequence = (QuantumGate*)malloc(bytes);
    if (!quantum_gate_host_sequence) {
        fprintf(stderr, "[C] Quantum: Failed to allocate host gate sequence (%zu bytes).\n", bytes);
        quantum_gate_host_count = 0;
        quantum_gate_sequence_bytes = 0;
        return 0;
    }
    memcpy(quantum_gate_host_sequence, gates, bytes);
    quantum_gate_host_count = (size_t)gate_count;
    quantum_gate_sequence_bytes = bytes;
    quantum_gate_sequence_last_qubits = 0;

    cl_int err = CL_SUCCESS;
    quantum_gate_sequence_buffer = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR,
                                                  bytes, (void*)gates, &err);
    if (!quantum_gate_sequence_buffer || err != CL_SUCCESS) {
        if (quantum_gate_sequence_buffer) {
            clReleaseMemObject(quantum_gate_sequence_buffer);
            quantum_gate_sequence_buffer = NULL;
        }
        fprintf(stderr, "[C] Quantum: Warning - Failed to create device gate sequence buffer: %s (%d). Using host path only.\n",
                clGetErrorString(err), err);
    }

    return 1;
}

DLLEXPORT int quantum_apply_gate_sequence(int gpu_index, int num_qubits, float* out_probabilities, int probability_length) {
    (void)gpu_index;
    if (num_qubits <= 0) {
        fprintf(stderr, "[C] Quantum: Invalid qubit count %d for gate sequence.\n", num_qubits);
        return 0;
    }
    size_t dimension = (size_t)1 << num_qubits;
    if (out_probabilities && probability_length > 0 && (size_t)probability_length < dimension) {
        fprintf(stderr, "[C] Quantum: Probability buffer too small (have %d need %zu).\n",
                probability_length, dimension);
        return 0;
    }
    if (!quantum_gate_host_sequence || quantum_gate_host_count == 0) {
        fprintf(stderr, "[C] Quantum: No gate sequence uploaded.\n");
        return 0;
    }
    if (!ensure_quantum_kernels_ready()) {
        return 0;
    }

    QuantumStateGPU state = {0};
    cl_float2* host_state = NULL;
    cl_mem probabilities = NULL;
    float* host_probs = NULL;
    cl_int err = CL_SUCCESS;
    int success = 0;

    if (!quantum_allocate_state(num_qubits, &state)) {
        goto cleanup;
    }

    host_state = (cl_float2*)calloc(state.dimension, sizeof(cl_float2));
    if (!host_state) {
        fprintf(stderr, "[C] Quantum: Failed to allocate host state buffer (%zu bytes).\n",
                state.dimension * sizeof(cl_float2));
        goto cleanup;
    }
    host_state[0] = make_complex(1.0f, 0.0f);

    for (size_t i = 0; i < quantum_gate_host_count; ++i) {
        if (!quantum_apply_gate_cpu(host_state, num_qubits, &quantum_gate_host_sequence[i])) {
            goto cleanup;
        }
    }

    err = clEnqueueWriteBuffer(queue, state.buffer, CL_TRUE, 0,
                                state.dimension * sizeof(cl_float2), host_state, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to upload state after gate sequence: %s (%d)\n",
                clGetErrorString(err), err);
        goto cleanup;
    }

    if (!quantum_compute_probabilities_gpu(&state, &probabilities)) {
        goto cleanup;
    }

    host_probs = (float*)malloc(dimension * sizeof(float));
    if (!host_probs) {
        fprintf(stderr, "[C] Quantum: Failed to allocate host probability buffer (%zu bytes).\n",
                dimension * sizeof(float));
        goto cleanup;
    }

    err = clEnqueueReadBuffer(queue, probabilities, CL_TRUE, 0,
                              dimension * sizeof(float), host_probs, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to read probability buffer: %s (%d)\n",
                clGetErrorString(err), err);
        goto cleanup;
    }

    if (out_probabilities) {
        memcpy(out_probabilities, host_probs, dimension * sizeof(float));
    }
    success = 1;
    quantum_gate_sequence_last_qubits = num_qubits;

cleanup:
    if (probabilities) {
        clReleaseMemObject(probabilities);
    }
    if (host_probs) {
        free(host_probs);
    }
    if (host_state) {
        free(host_state);
    }
    quantum_release_state(&state);
    return success;
}

static int infer_gate_sequence_qubits(void) {
    int max_qubit = -1;
    if (!quantum_gate_host_sequence || quantum_gate_host_count == 0) {
        return 0;
    }
    for (size_t i = 0; i < quantum_gate_host_count; ++i) {
        const QuantumGate* gate = &quantum_gate_host_sequence[i];
        int indices[3] = {(int)gate->target, (int)gate->control, (int)gate->control2};
        for (int j = 0; j < 3; ++j) {
            if (indices[j] > max_qubit) {
                max_qubit = indices[j];
            }
        }
    }
    return max_qubit >= 0 ? (max_qubit + 1) : 0;
}

DLLEXPORT int quantum_export_to_qasm(int gpu_index, const char* filepath) {
    (void)gpu_index;
    if (!filepath || !quantum_gate_host_sequence || quantum_gate_host_count == 0) {
        fprintf(stderr, "[C] Quantum: Cannot export QASM – missing filepath or gate sequence.\n");
        return 0;
    }

    int num_qubits = quantum_gate_sequence_last_qubits;
    if (num_qubits <= 0) {
        num_qubits = infer_gate_sequence_qubits();
    }
    if (num_qubits <= 0) {
        fprintf(stderr, "[C] Quantum: Unable to infer qubit count for QASM export.\n");
        return 0;
    }

    FILE* fp = fopen(filepath, "w");
    if (!fp) {
        fprintf(stderr, "[C] Quantum: Failed to open QASM file '%s' for writing.\n", filepath);
        return 0;
    }

    fprintf(fp, "OPENQASM 2.0;\ninclude \"qelib1.inc\";\n");
    fprintf(fp, "qreg q[%d];\n", num_qubits);

    for (size_t i = 0; i < quantum_gate_host_count; ++i) {
        const QuantumGate* gate = &quantum_gate_host_sequence[i];
        const char* name = gate->name;
        if (!name) { continue; }

        if (strncmp(name, "U3", 2) == 0 || strncmp(name, "u3", 2) == 0) {
            float theta = gate->params[0];
            float phi = gate->params[1];
            float lambda = gate->params[2];
            fprintf(fp, "u3(%f,%f,%f) q[%u];\n", theta, phi, lambda, gate->target);
        } else if (strncmp(name, "CRZ", 3) == 0 || strncmp(name, "crz", 3) == 0) {
            float theta = gate->params[0];
            fprintf(fp, "crz(%f) q[%u],q[%u];\n", theta, gate->control, gate->target);
        } else if (strncmp(name, "SWAP", 4) == 0 || strncmp(name, "swap", 4) == 0) {
            fprintf(fp, "swap q[%u],q[%u];\n", gate->control, gate->target);
        } else if (strncmp(name, "TOFF", 4) == 0 || strncmp(name, "ccx", 3) == 0) {
            fprintf(fp, "ccx q[%u],q[%u],q[%u];\n", gate->control, gate->control2, gate->target);
        } else {
            fprintf(fp, "// Unsupported gate '%s'\n", name);
        }
    }

    fclose(fp);
    return 1;
}

DLLEXPORT int quantum_import_from_qasm(const char* filepath,
                                       QuantumGate* out_gates,
                                       int max_gates,
                                       int* out_gate_count,
                                       int* out_num_qubits) {
    if (!filepath || !out_gates || max_gates <= 0 || !out_gate_count || !out_num_qubits) {
        fprintf(stderr, "[C] Quantum: Invalid arguments for quantum_import_from_qasm.\n");
        return 0;
    }

    FILE* fp = fopen(filepath, "r");
    if (!fp) {
        fprintf(stderr, "[C] Quantum: Unable to open QASM file '%s' for reading.\n", filepath);
        return 0;
    }

    char line[512];
    int gate_count = 0;
    int num_qubits = 0;
    while (fgets(line, sizeof(line), fp)) {
        char* trimmed = trim_whitespace(line);
        if (!trimmed || trimmed[0] == '\0' || is_line_comment(trimmed)) {
            continue;
        }
        if (cc_strncasecmp(trimmed, "OPENQASM", 8) == 0 || cc_strncasecmp(trimmed, "INCLUDE", 7) == 0) {
            continue;
        }
        if (cc_strncasecmp(trimmed, "QREG", 4) == 0) {
            char* bracket = strchr(trimmed, '[');
            char* close = bracket ? strchr(bracket, ']') : NULL;
            if (bracket && close && close > bracket + 1) {
                char number[32];
                size_t len = (size_t)(close - bracket - 1);
                if (len < sizeof(number)) {
                    memcpy(number, bracket + 1, len);
                    number[len] = '\0';
                    num_qubits = atoi(number);
                }
            }
            continue;
        }

        char* semicolon = strchr(trimmed, ';');
        if (semicolon) { *semicolon = '\0'; }

        char gate_token[64] = {0};
        size_t idx = 0;
        while (trimmed[idx] && !isspace((unsigned char)trimmed[idx]) && trimmed[idx] != '(') {
            gate_token[idx] = (char)toupper((unsigned char)trimmed[idx]);
            ++idx;
        }
        gate_token[idx] = '\0';
        const char* rest = trimmed + idx;
        while (*rest && isspace((unsigned char)*rest)) { ++rest; }

        const char* param_start = strchr(trimmed, '(');
        const char* param_end = param_start ? strchr(param_start, ')') : NULL;
        char param_buf[128] = {0};
        if (param_start && param_end && param_end > param_start) {
            size_t len = (size_t)(param_end - param_start + 1);
            if (len >= sizeof(param_buf)) { len = sizeof(param_buf) - 1; }
            memcpy(param_buf, param_start, len);
            param_buf[len] = '\0';
            rest = param_end + 1;
            while (*rest && isspace((unsigned char)*rest)) { ++rest; }
        }

        QuantumGate gate;
        quantum_gate_init(&gate, gate_token);
        gate.arity = 1;
        gate.target = 0;
        gate.control = 0;
        gate.control2 = 0;

        char args_copy[128] = {0};
        strncpy(args_copy, rest, sizeof(args_copy) - 1);
        char* arg_token = strtok(args_copy, ",");
        char* next_token = NULL;
        int target = 0;
        int control = 0;
        int control2 = 0;

        if (arg_token) {
            char* trimmed_arg = trim_whitespace(arg_token);
            quantum_parse_qubit_index(trimmed_arg, &target);
            next_token = strtok(NULL, ",");
        }
        if (next_token) {
            char* trimmed_ctrl = trim_whitespace(next_token);
            quantum_parse_qubit_index(trimmed_ctrl, &control);
            char* token3 = strtok(NULL, ",");
            if (token3) {
                char* trimmed_ctrl2 = trim_whitespace(token3);
                quantum_parse_qubit_index(trimmed_ctrl2, &control2);
            }
        }

        int appended = 0;
        if (strcmp(gate_token, "H") == 0 || strcmp(gate_token, "X") == 0 ||
            strcmp(gate_token, "Y") == 0 || strcmp(gate_token, "Z") == 0) {
            gate.target = (cl_uint)target;
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "RX") == 0 || strcmp(gate_token, "RY") == 0 || strcmp(gate_token, "RZ") == 0) {
            float angle = 0.0f;
            if (!quantum_parse_float(param_buf[0] ? param_buf + 1 : rest, &angle)) {
                fprintf(stderr, "[C] Quantum: Failed to parse angle for %s gate.\n", gate_token);
                fclose(fp);
                return 0;
            }
            gate.target = (cl_uint)target;
            gate.params[0] = angle;
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "CX") == 0 || strcmp(gate_token, "CNOT") == 0) {
            gate.arity = 2;
            gate.control = (cl_uint)target;
            gate.target = (cl_uint)control;
            strncpy(gate.name, "CNOT", sizeof(gate.name) - 1);
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "CZ") == 0) {
            gate.arity = 2;
            gate.control = (cl_uint)target;
            gate.target = (cl_uint)control;
            strncpy(gate.name, "CPHASE", sizeof(gate.name) - 1);
            gate.params[0] = (float)M_PI;
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "SWAP") == 0) {
            gate.arity = 2;
            gate.control = (cl_uint)target;
            gate.target = (cl_uint)control;
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "CCX") == 0 || strcmp(gate_token, "TOFF") == 0 || strcmp(gate_token, "CCNOT") == 0) {
            gate.arity = 3;
            gate.control = (cl_uint)target;
            gate.control2 = (cl_uint)control;
            gate.target = (cl_uint)control2;
            strncpy(gate.name, "CCX", sizeof(gate.name) - 1);
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "CRZ") == 0 || strcmp(gate_token, "CRX") == 0 || strcmp(gate_token, "CRY") == 0) {
            float angle = 0.0f;
            if (!quantum_parse_float(param_buf[0] ? param_buf + 1 : rest, &angle)) {
                fprintf(stderr, "[C] Quantum: Failed to parse angle for %s gate.\n", gate_token);
                fclose(fp);
                return 0;
            }
            gate.arity = 2;
            gate.control = (cl_uint)target;
            gate.target = (cl_uint)control;
            gate.params[0] = angle;
            appended = quantum_append_gate(out_gates, max_gates, &gate_count, &gate);
        } else if (strcmp(gate_token, "U3") == 0) {
            float params[3];
            if (!quantum_parse_three_floats(param_buf, params)) {
                fprintf(stderr, "[C] Quantum: Failed to parse U3 parameters.\n");
                fclose(fp);
                return 0;
            }
            QuantumGate rz1, ry, rz2;
            quantum_gate_init(&rz1, "RZ");
            quantum_gate_init(&ry, "RY");
            quantum_gate_init(&rz2, "RZ");
            rz1.target = (cl_uint)target;
            ry.target = (cl_uint)target;
            rz2.target = (cl_uint)target;
            rz1.params[0] = params[1];
            ry.params[0] = params[0];
            rz2.params[0] = params[2];
            if (!quantum_append_gate(out_gates, max_gates, &gate_count, &rz1) ||
                !quantum_append_gate(out_gates, max_gates, &gate_count, &ry) ||
                !quantum_append_gate(out_gates, max_gates, &gate_count, &rz2)) {
                fprintf(stderr, "[C] Quantum: Not enough space to expand U3 gate.\n");
                fclose(fp);
                return 0;
            }
            appended = 1;
        } else {
            fprintf(stderr, "[C] Quantum: Unsupported QASM gate '%s'.\n", gate_token);
            fclose(fp);
            return 0;
        }

        if (!appended) {
            fprintf(stderr, "[C] Quantum: Failed to append gate '%s' during QASM import.\n", gate_token);
            fclose(fp);
            return 0;
        }
    }

    fclose(fp);
    *out_gate_count = gate_count;
    *out_num_qubits = num_qubits;
    return 1;
}

DLLEXPORT int execute_quantum_echoes_otoc_gpu(
    int gpu_index,
    int num_qubits,
    const QuantumGate* U_gates,
    int U_gate_count,
    const QuantumGate* W_gate,
    const QuantumGate* V_gate,
    int measure_otoc2,
    float* out_L,
    float* out_otoc2_real,
    float* out_otoc2_imag) {
    int success = 0;
    int have_echo_state = 0;
    int have_otoc_state = 0;
    QuantumEchoProfile profile = {0};
    QuantumStateGPU echo_state = {0};
    QuantumStateGPU otoc_state = {0};
    cl_float2 amp0;
    cl_float2 amp_otoc;
    cl_float2 stack_amp;
    cl_float2* amp_target = NULL;
    cl_float2 stack_otoc;
    cl_float2* otoc_target = NULL;
    cl_int err = CL_SUCCESS;
    cl_command_queue active_queue = queue;
    GpuSlot* slot = cc_get_slot(gpu_index);
    if (slot && slot->queue) {
        active_queue = slot->queue;
    }
    profile.used_out_of_order_queue = (slot && slot->out_of_order_enabled) ? 1 : 0;
    double start_ms = cc_now_ms();
    g_active_quantum_profile = &profile;

    if (num_qubits <= 0) {
        fprintf(stderr, "[C] Quantum Echoes: Invalid qubit count %d.\n", num_qubits);
        goto cleanup;
    }
    if (U_gate_count < 0) {
        fprintf(stderr, "[C] Quantum Echoes: Invalid gate count %d.\n", U_gate_count);
        goto cleanup;
    }
    if (U_gate_count > 0 && !U_gates) {
        fprintf(stderr, "[C] Quantum Echoes: Gate list pointer is NULL while count is %d.\n", U_gate_count);
        goto cleanup;
    }
    if (!W_gate) {
        fprintf(stderr, "[C] Quantum Echoes: Perturbation gate W is NULL.\n");
        goto cleanup;
    }
    if (!out_L) {
        fprintf(stderr, "[C] Quantum Echoes: Output pointer for L is NULL.\n");
        goto cleanup;
    }
    if (measure_otoc2 && (!out_otoc2_real || !out_otoc2_imag)) {
        fprintf(stderr, "[C] Quantum Echoes: OTOC(2) requested but output pointers are NULL.\n");
        goto cleanup;
    }
    if (out_otoc2_real) { *out_otoc2_real = 0.0f; }
    if (out_otoc2_imag) { *out_otoc2_imag = 0.0f; }
    amp0.s[0] = 0.0f;
    amp0.s[1] = 0.0f;
    amp_otoc.s[0] = 0.0f;
    amp_otoc.s[1] = 0.0f;
    stack_amp.s[0] = 0.0f;
    stack_amp.s[1] = 0.0f;
    stack_otoc.s[0] = 0.0f;
    stack_otoc.s[1] = 0.0f;

    // TODO: Multi-Device: Full per-device kernel compilation is pending; we currently map the queue via cc_get_slot when available.
    if (!ensure_quantum_kernels_ready()) {
        goto cleanup;
    }
    if (!quantum_allocate_state(num_qubits, &echo_state)) {
        goto cleanup;
    }
    have_echo_state = 1;
    if (!quantum_initialize_zero_state(&echo_state)) {
        goto cleanup;
    }
    if (U_gate_count > 0) {
        if (!quantum_apply_sequence(&echo_state, U_gates, U_gate_count)) {
            goto cleanup;
        }
    }
    if (!quantum_apply_gate_from_desc(&echo_state, W_gate)) {
        goto cleanup;
    }
    if (U_gate_count > 0) {
        if (!quantum_apply_sequence_dagger(&echo_state, U_gates, U_gate_count)) {
            goto cleanup;
        }
    }

#ifndef NDEBUG
    if (!quantum_check_norm1(gpu_index, &echo_state, 1e-3f, "Echo final")) {
        goto cleanup;
    }
#endif

    amp_target = (slot && slot->pinned_amp_host) ? slot->pinned_amp_host : &stack_amp;
    err = clEnqueueReadBuffer(active_queue, echo_state.buffer, CL_TRUE, 0,
                              sizeof(cl_float2), amp_target, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum Echoes: Failed to read amplitude 0: %s (%d).\n", clGetErrorString(err), err);
        goto cleanup;
    }
    if (amp_target != &stack_amp) {
        amp0 = *amp_target;
    } else {
        amp0 = stack_amp;
    }
    *out_L = amp0.s[0] * amp0.s[0] + amp0.s[1] * amp0.s[1];

    if (measure_otoc2) {
        if (!quantum_allocate_state(num_qubits, &otoc_state)) {
            goto cleanup;
        }
        have_otoc_state = 1;
        if (!quantum_initialize_zero_state(&otoc_state)) {
            goto cleanup;
        }
        if (U_gate_count > 0 && !quantum_apply_sequence(&otoc_state, U_gates, U_gate_count)) {
            goto cleanup;
        }
        if (!quantum_apply_gate_from_desc(&otoc_state, W_gate)) {
            goto cleanup;
        }
        if (U_gate_count > 0 && !quantum_apply_sequence_dagger(&otoc_state, U_gates, U_gate_count)) {
            goto cleanup;
        }
        if (V_gate && !quantum_apply_gate_from_desc(&otoc_state, V_gate)) {
            goto cleanup;
        }
        if (U_gate_count > 0 && !quantum_apply_sequence(&otoc_state, U_gates, U_gate_count)) {
            goto cleanup;
        }
        if (!quantum_apply_gate_dagger(&otoc_state, W_gate)) {
            goto cleanup;
        }
        if (U_gate_count > 0 && !quantum_apply_sequence_dagger(&otoc_state, U_gates, U_gate_count)) {
            goto cleanup;
        }
        if (V_gate && !quantum_apply_gate_dagger(&otoc_state, V_gate)) {
            goto cleanup;
        }

#ifndef NDEBUG
        if (!quantum_check_norm1(gpu_index, &otoc_state, 1e-3f, "OTOC final")) {
            goto cleanup;
        }
#endif

        otoc_target = (slot && slot->pinned_amp_host) ? slot->pinned_amp_host : &stack_otoc;
        err = clEnqueueReadBuffer(active_queue, otoc_state.buffer, CL_TRUE, 0,
                                   sizeof(cl_float2), otoc_target, 0, NULL, NULL);
        if (err != CL_SUCCESS) {
            fprintf(stderr, "[C] Quantum Echoes: Failed to read OTOC amplitude: %s (%d).\n", clGetErrorString(err), err);
            goto cleanup;
        }
        if (otoc_target != &stack_otoc) {
            amp_otoc = *otoc_target;
        } else {
            amp_otoc = stack_otoc;
        }
        *out_otoc2_real = amp_otoc.s[0];
        *out_otoc2_imag = amp_otoc.s[1];
    } else {
        if (out_otoc2_real) { *out_otoc2_real = 0.0f; }
        if (out_otoc2_imag) { *out_otoc2_imag = 0.0f; }
    }

    success = 1;

cleanup:
    if (g_active_quantum_profile == &profile) {
        g_active_quantum_profile = NULL;
    }
    if (have_otoc_state) {
        quantum_release_state(&otoc_state);
    }
    if (have_echo_state) {
        quantum_release_state(&echo_state);
    }
    if (!finish_queue_and_check(gpu_index, "execute_quantum_echoes_otoc_gpu")) {
        success = 0;
    }
    profile.host_wall_time_ms = cc_now_ms() - start_ms;
    g_last_quantum_echo_profile = profile;
    return success;
}

/**
 * @brief Shuts down the OpenCL driver and releases all resources.
 */
DLLEXPORT void shutdown_gpu(int gpu_index) {
    printf("[C] shutdown_gpu: Received shutdown request for GPU index %d. Shutting down global OpenCL resources.\n", gpu_index);
    shutdown_driver();
}


// --- Command Data Structures (Used by submit_kernel_command) ---
typedef struct { void* buffer_a; void* buffer_b; void* buffer_c; int B; int M; int N; int K; } BMMCommandData;
typedef struct { void* buffer_input; void* buffer_output; int num_rows; int row_size; } SoftmaxCommandData;
typedef struct { void* buffer_input; void* buffer_output; int num_elements; } GeluCommandData;
typedef struct { void* buffer_a; void* buffer_b; void* buffer_c; int num_elements; } AddCommandData;
typedef struct { void* buffer_a; void* buffer_b; void* buffer_c; int num_elements; } MulCommandData;
typedef struct { void* buffer_input; void* buffer_output; int num_rows; int row_size; float eps; } LayerNormCommandData;
typedef struct { void* src_buffer; void* dst_buffer; size_t size; } CloneCommandData;
typedef struct { void* buffer_input; void* buffer_output; int rows; int cols; } TransposeCommandData;
typedef struct { void* buffer_input; void* buffer_grad_output; void* buffer_grad_input; int num_elements; } GeluBackwardCommandData;
typedef struct { void* buffer_a; void* buffer_b; void* buffer_dc; void* buffer_da; void* buffer_db; int B, M, N, K; } MatMulBackwardData;
typedef struct { void* buffer_dy; void* buffer_x; void* buffer_dx; int num_rows; int row_size; float eps; } LayerNormBackwardCommandData;
typedef struct { void* param_buffer; void* grad_buffer; void* m_buffer; void* v_buffer; int num_elements; int t_step; float lr,beta1,beta2,eps,weight_decay,beta1_t,beta2_t; } AdamCommandData;
typedef struct { void* buffer_dy; void* buffer_y; void* buffer_dx; int num_rows; int row_size; } SoftmaxBackwardCommandData;
typedef struct { void* buffer_dC; void* buffer_A; void* buffer_B; void* buffer_dA; void* buffer_dB; int num_elements; } MulBackwardCommandData;
typedef struct { void* buffer_dC; void* buffer_dA; int rows_A; int cols_A; } TransposeBackwardCommandData;
typedef struct { void* idx; void* w; void* o; int b, s, d, v; } EmbeddingLookupCommandData;
typedef struct { void* in; void* out; int B, M, N; } ReduceSumCommandData;
typedef struct { void* a; void* b; void* c; int B, M, N; } BroadcastAddCommandData;
typedef struct { void* in; void* out; int B_flat, d1, d2; } TransposeBatchedCommandData;
typedef struct { void* in; void* out; int B, D1, D2, D3; } Transpose12BatchedCommandData;
typedef struct { void* buffer_a; void* buffer_b; void* buffer_c; int B; int M; int N; int K; } BMMBatchedCommandData;
typedef struct { void* buffer_a; void* buffer_b; void* buffer_dc; void* buffer_da; void* buffer_db; int B, M, N, K; } BMMBatchedBackwardData;
typedef struct { void* input_logits; void* output_log_probs; int B_S_rows; int V_cols; } LogSoftmaxStableCommandData;
typedef struct { void* log_probs; void* target_indices; void* grad_input; void* loss_per_sample; int B_S_rows; int V_cols; } CrossEntropyLossGradCommandData;typedef struct { void* input; void* pe_slice; void* output; int B; int S; int E; } AddBroadcastPECommandData;
typedef struct { void* buffer_a; void* buffer_c; void* buffer_w; float learning_rate; int B; int M; int N; int K; } HebbianUpdateLocalReduceCommandData;
typedef struct { void* buffer_activations; void* buffer_spikes; float threshold; int num_elements; } ThresholdSpikeCommandData;
typedef struct { void* a_or_c; void* b_bias; int M; int N; } AddBiasMNCommandData;
typedef struct { void* d_o; void* idx; void* delta_dw; int b; int s; int d; int v; } EmbeddingBackwardPass1CommandData;
typedef struct { void* activations_bse; void* prototypes_te; void* output_indices_bs; int B; int S; int E; int T; } DynamicTokenAssignmentCommandData;
typedef struct { void* states_nd; void* output_similarity_nn; int N; int D; } PairwiseSimilarityCommandData;
typedef struct {
    void* buffer_X;
    void* buffer_W;
    void* buffer_O;
    int B;
    int N;
    int D;
    float gamma;
    float sigma;
} FusedDiffusionCommandData;
typedef struct {
    void* v;
    void* u;
    void* i_inj;
    void* spikes_out;
    void* p_a;
    void* p_b;
    void* p_c;
    void* p_d;
    float dt;
    float threshold;
    int num_neurons;
} IzhikevichCommandData;
typedef struct {
    void* weights;
    void* pre_traces;
    void* post_traces;
    void* pre_spike_events;
    void* post_spike_events;
    float lr_ltp;
    float lr_ltd;
    int pre_n;
    int post_n;
} STDPUpdateCommandData;
typedef struct {
    void* pre_traces;
    void* post_traces;
    void* pre_spike_events;
    void* post_spike_events;
    float decay_pre;
    float decay_post;
    float increment_pre;
    float increment_post;
    int pre_n;
    int post_n;
} STDPTraceCommandData;
typedef struct {
    void* input;
    void* weights;
    void* bias;
    void* output;
    int B;
    int C_in;
    int H;
    int W;
    int C_out;
    int K_h;
    int K_w;
    int stride_h;
    int stride_w;
    int out_h;
    int out_w;
} Conv2DForwardCommandData;
typedef struct {
    void* grad_output;
    void* input;
    void* weights;
    void* grad_input;
    void* grad_weights;
    void* grad_bias;
    int B;
    int C_in;
    int H;
    int W;
    int C_out;
    int K_h;
    int K_w;
    int stride_h;
    int stride_w;
    int out_h;
    int out_w;
} Conv2DBackwardCommandData;
typedef struct {
    void* input;
    void* output;
    int B;
    int C;
    int H;
    int W;
} PatchPermuteCommandData;
typedef struct {
    void* f_in;
    void* f_out;
    void* rho;
    void* ux;
    void* uy;
    float omega;
    int width;
    int height;
} LBMCollideStreamCommandData;
typedef struct {
    void* positions;
    void* forces;
    float gravitational_const;
    float softening_factor;
    int num_bodies;
} NBodyForcesCommandData;
typedef struct {
    void* positions;
    void* velocities;
    void* forces;
    float dt;
    int num_bodies;
} NBodyIntegrateCommandData;
typedef struct {
    void* spin_grid;
    void* random_numbers;
    float J;
    float beta;
    int width;
    int height;
    int color;
} IsingMetropolisCommandData;
typedef struct {
    void* activations_flat; void* indices_flat; void* proto_sums; void* proto_counts;
    int M_flat; int E; int T;
} ProtoSegmentedSumCommandData;
typedef struct {
    void* prototypes; void* proto_sums; void* proto_counts;
    float learning_rate; int E; int T;
} ProtoUpdateStepCommandData;
// Struct for Loss Shaping Kernel (Single Pair)
typedef struct {
    void* loss_per_sample_in;
    void* predictions;
    void* targets;
    void* loss_per_sample_out;
    int num_samples;
    int num_classes;
    float penalty_weight;
    float reward_weight;
    float high_confidence_threshold;
    int critical_target_class;
    int critical_predicted_class;
} ShapeLossRewardPenaltyCommandData;
// NEU: Struct for Loss Shaping Kernel (List of Pairs)
typedef struct {
    void* loss_per_sample_in;
    void* predictions;
    void* targets;
    void* loss_per_sample_out;
    void* critical_pairs; // Handle zum Buffer der ID-Paare
    int num_samples;
    int num_classes;
    int num_critical_pairs; // Anzahl der Paare
    float penalty_weight;
    float reward_weight;
    float high_confidence_threshold;
} ShapeLossRewardPenaltyListCommandData;

/**
 * @brief Zeros out a specified number of bytes in a GPU buffer.
 */
int zero_gpu_buffer(int gpu_index, void* gpu_buffer_handle, size_t size_bytes) {
    FP_TYPE* zeros_host = NULL;
    size_t num_elements;
    int success = 1;

    if (!gpu_buffer_handle) { fprintf(stderr, "[C] zero_gpu_buffer: Error - GPU buffer handle is NULL.\n"); return 0; }
    if (size_bytes == 0) { return 1; }
    if (size_bytes % sizeof(FP_TYPE) != 0) { fprintf(stderr, "[C] zero_gpu_buffer: Error - size_bytes %zu is not a multiple of FP_TYPE size %zu.\n", size_bytes, sizeof(FP_TYPE)); return 0; }
    num_elements = size_bytes / sizeof(FP_TYPE);

    zeros_host = (FP_TYPE*)malloc(size_bytes);
    if (!zeros_host) { fprintf(stderr, "[C] zero_gpu_buffer: Error - Failed to malloc %zu bytes for host zero buffer.\n", size_bytes); return 0; }

    for (size_t i = 0; i < num_elements; ++i) { zeros_host[i] = (FP_TYPE)0.0; }

    if (!write_host_to_gpu_blocking(gpu_index, gpu_buffer_handle, 0, size_bytes, zeros_host)) {
        fprintf(stderr, "[C] zero_gpu_buffer: Error - Failed to write zeros to GPU buffer.\n");
        success = 0;
    }

    free(zeros_host);
    return success;
}

/** @brief Default work-group size for reduction kernels. Can be tuned. */
#ifndef REDUCE_WG_SIZE
#define REDUCE_WG_SIZE 256
#endif

/**
 * @brief Helper function to determine parameters for reduction kernels.
 */
static cl_int get_reduction_params_helper(size_t* lws_out, size_t* local_mem_bytes_out) {
    *lws_out = REDUCE_WG_SIZE;
    *local_mem_bytes_out = 0;
    if (!device_id) { fprintf(stderr, "[C] ERROR (Reduction Setup): No device ID available.\n"); return CL_INVALID_DEVICE; }

    size_t max_wg_size = 0;
    cl_int lws_err = clGetDeviceInfo(device_id, CL_DEVICE_MAX_WORK_GROUP_SIZE, sizeof(size_t), &max_wg_size, NULL);
    if (lws_err == CL_SUCCESS) {
        if (*lws_out > max_wg_size) {
            fprintf(stderr, "[C] WARN (Reduction Setup): Requested LWS %zu exceeds device max %zu, clamping LWS to %zu.\n", *lws_out, max_wg_size, max_wg_size);
            *lws_out = max_wg_size;
        }
    } else {
         fprintf(stderr, "[C] WARN (Reduction Setup): Failed to query max WGS (%s), using default LWS %zu without clamping check.\n", clGetErrorString(lws_err), *lws_out);
    }
    if (*lws_out == 0) { fprintf(stderr, "[C] ERROR (Reduction Setup): Calculated Local Work Size (LWS) is zero.\n"); return CL_INVALID_WORK_GROUP_SIZE; }

    #ifdef CL_HAS_FP64
        typedef double REDUCE_ACCUM_TYPE_HOST;
    #else
        typedef float REDUCE_ACCUM_TYPE_HOST;
    #endif
    *local_mem_bytes_out = (*lws_out) * sizeof(REDUCE_ACCUM_TYPE_HOST);

    cl_ulong max_lmem_size_ulong = 0;
    cl_int lmem_err = clGetDeviceInfo(device_id, CL_DEVICE_LOCAL_MEM_SIZE, sizeof(cl_ulong), &max_lmem_size_ulong, NULL);
    if (lmem_err == CL_SUCCESS) {
         if (*local_mem_bytes_out > (size_t)max_lmem_size_ulong) {
             fprintf(stderr, "[C] ERROR (Reduction Setup): Calculated local memory size %zu bytes exceeds device max %llu bytes for LWS %zu.\n",
                     *local_mem_bytes_out, (unsigned long long)max_lmem_size_ulong, *lws_out);
             return CL_INVALID_WORK_GROUP_SIZE;
         }
     } else {
         fprintf(stderr, "[C] WARN (Reduction Setup): Failed to query CL_DEVICE_LOCAL_MEM_SIZE (%s), cannot verify limit for %zu bytes needed.\n", clGetErrorString(lmem_err), *local_mem_bytes_out);
     }
    return CL_SUCCESS;
}

static cl_int enqueue_kernel_with_metrics(cl_kernel kernel,
                                          cl_uint work_dim,
                                          const size_t* global_work_size,
                                          const size_t* local_work_size,
                                          const char* kernel_name,
                                          float* error_out,
                                          float* variance_out) {
    cl_command_queue active_queue = g_thread_queue ? g_thread_queue : queue;
    if (!active_queue) {
        cc_set_last_error("[C] enqueue_kernel_with_metrics: No active command queue available");
        return CL_INVALID_COMMAND_QUEUE;
    }

    cl_event evt = NULL;
    cl_int err = clEnqueueNDRangeKernel(active_queue, kernel, work_dim, NULL,
                                        global_work_size, local_work_size,
                                        0, NULL, &evt);

    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] enqueue_kernel_with_metrics: Failed to launch %s: %s (%d)\n",
                kernel_name ? kernel_name : "<unknown>", clGetErrorString(err), err);
        cc_set_last_error("[C] enqueue_kernel_with_metrics: Failed to launch %s: %s (%d)",
                          kernel_name ? kernel_name : "<unknown>", clGetErrorString(err), err);
        if (evt) {
            clReleaseEvent(evt);
        }
        return err;
    }

    if (g_force_kernel_finish) {
        if (evt) {
            clWaitForEvents(1, &evt);
        } else {
            clFinish(active_queue);
        }

        cl_ulong start_time = 0;
        cl_ulong end_time = 0;
        if (evt) {
            clGetEventProfilingInfo(evt, CL_PROFILING_COMMAND_START, sizeof(start_time), &start_time, NULL);
            clGetEventProfilingInfo(evt, CL_PROFILING_COMMAND_END, sizeof(end_time), &end_time, NULL);
        }

        double duration_ns = (double)(end_time > start_time ? (end_time - start_time) : 0ULL);
        float duration_ms = (float)(duration_ns * 1e-6);
        if (duration_ms <= 0.0f) {
            duration_ms = 0.01f;
        }

        float local_variance = duration_ms * 0.001f * get_noise_factor();
        if (local_variance < 1e-6f) {
            local_variance = 1e-6f;
        }
        float local_error = 0.0f;
        noisectrl_measure(local_variance, &local_error, &local_variance);

        if (error_out) { *error_out = local_error; }
        if (variance_out) { *variance_out = local_variance; }
        if (g_measurement_error_target) { *g_measurement_error_target = local_error; }
        if (g_measurement_variance_target) { *g_measurement_variance_target = local_variance; }

        if (kernel_name) {
            strncpy(g_last_metrics.name, kernel_name, sizeof(g_last_metrics.name) - 1);
            g_last_metrics.name[sizeof(g_last_metrics.name) - 1] = '\0';
        } else {
            strncpy(g_last_metrics.name, "<unnamed>", sizeof(g_last_metrics.name) - 1);
            g_last_metrics.name[sizeof(g_last_metrics.name) - 1] = '\0';
        }
        g_last_metrics.duration_ms = duration_ms;
        g_last_metrics.error = local_error;
        g_last_metrics.variance = local_variance;

        printf("[C] Kernel %s took %.3f ms (variance=%.5f, noise=%.3f)\n",
               g_last_metrics.name, duration_ms, local_variance, get_noise_factor());
    } else {
        if (error_out) { *error_out = 0.0f; }
        if (variance_out) { *variance_out = 0.0f; }
        if (g_measurement_error_target) { *g_measurement_error_target = 0.0f; }
        if (g_measurement_variance_target) { *g_measurement_variance_target = 0.0f; }

        if (kernel_name) {
            strncpy(g_last_metrics.name, kernel_name, sizeof(g_last_metrics.name) - 1);
            g_last_metrics.name[sizeof(g_last_metrics.name) - 1] = '\0';
        } else {
            strncpy(g_last_metrics.name, "<unnamed>", sizeof(g_last_metrics.name) - 1);
            g_last_metrics.name[sizeof(g_last_metrics.name) - 1] = '\0';
        }
        g_last_metrics.duration_ms = 0.0f;
        g_last_metrics.error = 0.0f;
        g_last_metrics.variance = 0.0f;
    }

    if (evt) {
        clReleaseEvent(evt);
    }

    return CL_SUCCESS;
}

static void release_subqg_resources(void) {
    if (!subqg_state_initialized) {
        return;
    }

    #define RELEASE_SUBQG_BUFFER(buf) \
        if (buf) { \
            clReleaseMemObject(buf); \
            buf = NULL; \
        }

    RELEASE_SUBQG_BUFFER(subqg_energy_buffer);
    RELEASE_SUBQG_BUFFER(subqg_phase_buffer);
    RELEASE_SUBQG_BUFFER(subqg_interference_buffer);
    RELEASE_SUBQG_BUFFER(subqg_node_flag_buffer);
    RELEASE_SUBQG_BUFFER(subqg_spin_buffer);
    RELEASE_SUBQG_BUFFER(subqg_topology_buffer);
    RELEASE_SUBQG_BUFFER(subqg_rng_energy_buffer);
    RELEASE_SUBQG_BUFFER(subqg_rng_phase_buffer);
    RELEASE_SUBQG_BUFFER(subqg_rng_spin_buffer);
    RELEASE_SUBQG_BUFFER(subqg_field_map_buffer);
    RELEASE_SUBQG_BUFFER(subqg_agent_buffer);

    #undef RELEASE_SUBQG_BUFFER

    subqg_noise_level = 0.0f;
    subqg_threshold = 0.0f;
    subqg_cell_count = 0;
    subqg_rng_seed = 0;
    subqg_rng_state = 0;
    subqg_deterministic_mode = 0;
    subqg_state_initialized = 0;
    subqg_field_map_elements = 0;
    subqg_width = 0;
    subqg_height = 0;
    subqg_agent_buffer_bytes = 0;
}

static void release_quantum_program_objects(void) {
    if (quantum_single_qubit_kernel) { clReleaseKernel(quantum_single_qubit_kernel); quantum_single_qubit_kernel = NULL; }
    if (quantum_controlled_phase_kernel) { clReleaseKernel(quantum_controlled_phase_kernel); quantum_controlled_phase_kernel = NULL; }
    if (quantum_controlled_not_kernel) { clReleaseKernel(quantum_controlled_not_kernel); quantum_controlled_not_kernel = NULL; }
    if (quantum_phase_oracle_kernel) { clReleaseKernel(quantum_phase_oracle_kernel); quantum_phase_oracle_kernel = NULL; }
    if (quantum_phase_zero_kernel) { clReleaseKernel(quantum_phase_zero_kernel); quantum_phase_zero_kernel = NULL; }
    if (quantum_modexp_kernel) { clReleaseKernel(quantum_modexp_kernel); quantum_modexp_kernel = NULL; }
    if (quantum_swap_kernel) { clReleaseKernel(quantum_swap_kernel); quantum_swap_kernel = NULL; }
    if (quantum_probability_kernel) { clReleaseKernel(quantum_probability_kernel); quantum_probability_kernel = NULL; }
    if (quantum_expectation_pauli_z_kernel) { clReleaseKernel(quantum_expectation_pauli_z_kernel); quantum_expectation_pauli_z_kernel = NULL; }
    if (quantum_apply_gate_kernel) { clReleaseKernel(quantum_apply_gate_kernel); quantum_apply_gate_kernel = NULL; }
    if (quantum_vqe_gradient_kernel) { clReleaseKernel(quantum_vqe_gradient_kernel); quantum_vqe_gradient_kernel = NULL; }
    if (qualia_resonator_kernel) { clReleaseKernel(qualia_resonator_kernel); qualia_resonator_kernel = NULL; }
    if (intuition_precognition_kernel) { clReleaseKernel(intuition_precognition_kernel); intuition_precognition_kernel = NULL; }
    if (context_resonance_kernel) { clReleaseKernel(context_resonance_kernel); context_resonance_kernel = NULL; }
    if (dream_state_generator_kernel) { clReleaseKernel(dream_state_generator_kernel); dream_state_generator_kernel = NULL; }
    if (transformation_planner_kernel) { clReleaseKernel(transformation_planner_kernel); transformation_planner_kernel = NULL; }
    if (system_narrative_kernel) { clReleaseKernel(system_narrative_kernel); system_narrative_kernel = NULL; }
    if (symbolic_abstraction_kernel) { clReleaseKernel(symbolic_abstraction_kernel); symbolic_abstraction_kernel = NULL; }
    if (quantum_program) { clReleaseProgram(quantum_program); quantum_program = NULL; }
}

static void release_quantum_resources(void) {
    if (quantum_temp_state_buffer) {
        clReleaseMemObject(quantum_temp_state_buffer);
        quantum_temp_state_buffer = NULL;
    }
    if (quantum_probability_buffer) {
        clReleaseMemObject(quantum_probability_buffer);
        quantum_probability_buffer = NULL;
    }
    if (quantum_gate_sequence_buffer) {
        clReleaseMemObject(quantum_gate_sequence_buffer);
        quantum_gate_sequence_buffer = NULL;
    }
    if (quantum_gate_host_sequence) {
        free(quantum_gate_host_sequence);
        quantum_gate_host_sequence = NULL;
    }
    quantum_temp_state_bytes = 0;
    quantum_probability_bytes = 0;
    quantum_gate_sequence_bytes = 0;
    quantum_gate_host_count = 0;
}

static cl_float2 make_complex(float real, float imag) {
    cl_float2 value;
    value.s[0] = real;
    value.s[1] = imag;
    return value;
}

static cl_float2 complex_add(cl_float2 a, cl_float2 b) {
    return make_complex(a.s[0] + b.s[0], a.s[1] + b.s[1]);
}

static cl_float2 complex_mul(cl_float2 a, cl_float2 b) {
    float real = a.s[0] * b.s[0] - a.s[1] * b.s[1];
    float imag = a.s[0] * b.s[1] + a.s[1] * b.s[0];
    return make_complex(real, imag);
}

static cl_float2 complex_zero(void) {
    return make_complex(0.0f, 0.0f);
}

static size_t apply_gate_compose_index(size_t base, const int* qubits, int arity, size_t local_index) {
    size_t idx = base;
    for (int bit = 0; bit < arity; ++bit) {
        size_t mask = (size_t)1 << qubits[bit];
        if ((local_index >> bit) & 1U) {
            idx |= mask;
        }
    }
    return idx;
}

static int quantum_apply_gate_cpu(cl_float2* state, int num_qubits, const QuantumGate* gate) {
    if (!state || !gate) { return 0; }
    int arity = (int)gate->arity;
    if (arity <= 0 || arity > 3) {
        fprintf(stderr, "[C] Quantum: Unsupported gate arity %d.\n", arity);
        return 0;
    }

    int qubits[3] = {0, 0, 0};
    if (arity >= 1) { qubits[0] = (int)gate->target; }
    if (arity >= 2) { qubits[1] = (int)gate->control; }
    if (arity >= 3) { qubits[2] = (int)gate->control2; }

    // Maintain order: for two-qubit gates default to control-target ordering
    if (arity == 2) {
        qubits[0] = (int)gate->control;
        qubits[1] = (int)gate->target;
    }
    if (arity == 3) {
        qubits[0] = (int)gate->control;
        qubits[1] = (int)gate->control2;
        qubits[2] = (int)gate->target;
    }

    for (int i = 0; i < arity; ++i) {
        if (qubits[i] < 0 || qubits[i] >= num_qubits) {
            fprintf(stderr, "[C] Quantum: Gate references invalid qubit index %d (num_qubits=%d).\n",
                    qubits[i], num_qubits);
            return 0;
        }
        for (int j = i + 1; j < arity; ++j) {
            if (qubits[i] == qubits[j]) {
                fprintf(stderr, "[C] Quantum: Gate references duplicate qubit index %d.\n", qubits[i]);
                return 0;
            }
        }
    }

    size_t dimension = (size_t)1 << num_qubits;
    size_t subspace = (size_t)1 << arity;
    size_t gate_mask = 0;
    for (int i = 0; i < arity; ++i) {
        gate_mask |= ((size_t)1 << qubits[i]);
    }

    cl_float2 input_vec[8];
    cl_float2 output_vec[8];

    for (size_t base = 0; base < dimension; ++base) {
        if ((base & gate_mask) != 0) {
            continue;
        }

        for (size_t col = 0; col < subspace; ++col) {
            size_t idx = apply_gate_compose_index(base, qubits, arity, col);
            input_vec[col] = state[idx];
        }

        for (size_t row = 0; row < subspace; ++row) {
            cl_float2 acc = complex_zero();
            for (size_t col = 0; col < subspace; ++col) {
                cl_float2 m = gate->matrix[row][col];
                acc = complex_add(acc, complex_mul(m, input_vec[col]));
            }
            output_vec[row] = acc;
        }

        for (size_t row = 0; row < subspace; ++row) {
            size_t idx = apply_gate_compose_index(base, qubits, arity, row);
            state[idx] = output_vec[row];
        }
    }

    return 1;
}

static int ensure_quantum_kernels_ready(void) {
    if (!g_quantum_enabled) {
        if (!g_quantum_disabled_warned) {
            fprintf(stderr, "[C] Quantum: Kernels disabled via configuration.\n");
            g_quantum_disabled_warned = 1;
        }
        return 0;
    }
    if (!context || !queue) {
        fprintf(stderr, "[C] Quantum: Context/queue not initialized. Call initialize_gpu first.\n");
        return 0;
    }
    if (!quantum_program || !quantum_single_qubit_kernel || !quantum_controlled_phase_kernel ||
        !quantum_controlled_not_kernel || !quantum_phase_oracle_kernel || !quantum_phase_zero_kernel ||
        !quantum_modexp_kernel || !quantum_swap_kernel || !quantum_probability_kernel ||
        !quantum_expectation_pauli_z_kernel || !quantum_vqe_gradient_kernel ||
        !qualia_resonator_kernel || !intuition_precognition_kernel || !context_resonance_kernel ||
        !dream_state_generator_kernel || !transformation_planner_kernel ||
        !system_narrative_kernel || !symbolic_abstraction_kernel) {
        fprintf(stderr, "[C] Quantum: Kernels not compiled. Ensure initialize_gpu succeeded.\n");
        return 0;
    }
    return 1;
}

static int quantum_reserve_temp_state(size_t dimension) {
    size_t required_bytes = dimension * sizeof(cl_float2);
    if (dimension == 0) { return 0; }
    if (quantum_temp_state_buffer && quantum_temp_state_bytes >= required_bytes) {
        return 1;
    }
    if (quantum_temp_state_buffer) {
        clReleaseMemObject(quantum_temp_state_buffer);
        quantum_temp_state_buffer = NULL;
        quantum_temp_state_bytes = 0;
    }
    cl_int err = CL_SUCCESS;
    quantum_temp_state_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, required_bytes, NULL, &err);
    if (!quantum_temp_state_buffer || err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to allocate temp state buffer (%zu bytes): %s (%d)\n",
                required_bytes, clGetErrorString(err), err);
        quantum_temp_state_buffer = NULL;
        return 0;
    }
    quantum_temp_state_bytes = required_bytes;
    return 1;
}

static int quantum_reserve_probability_buffer(size_t dimension) {
    size_t required_bytes = dimension * sizeof(cl_float);
    if (dimension == 0) { return 0; }
    if (quantum_probability_buffer && quantum_probability_bytes >= required_bytes) {
        return 1;
    }
    if (quantum_probability_buffer) {
        clReleaseMemObject(quantum_probability_buffer);
        quantum_probability_buffer = NULL;
        quantum_probability_bytes = 0;
    }
    cl_int err = CL_SUCCESS;
    quantum_probability_buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, required_bytes, NULL, &err);
    if (!quantum_probability_buffer || err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to allocate probability buffer (%zu bytes): %s (%d)\n",
                required_bytes, clGetErrorString(err), err);
        quantum_probability_buffer = NULL;
        return 0;
    }
    quantum_probability_bytes = required_bytes;
    return 1;
}

static int quantum_allocate_state(int num_qubits, QuantumStateGPU* state_out) {
    if (!state_out) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (num_qubits <= 0) {
        fprintf(stderr, "[C] Quantum: Requested invalid qubit count %d.\n", num_qubits);
        return 0;
    }
    size_t dimension = (size_t)1 << num_qubits;
    size_t bytes = dimension * sizeof(cl_float2);
    cl_int err = CL_SUCCESS;
    cl_mem buffer = clCreateBuffer(context, CL_MEM_READ_WRITE, bytes, NULL, &err);
    if (!buffer || err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to allocate state buffer (%zu bytes): %s (%d)\n",
                bytes, clGetErrorString(err), err);
        return 0;
    }
    state_out->buffer = buffer;
    state_out->num_qubits = num_qubits;
    state_out->dimension = dimension;
    if (!quantum_initialize_zero_state(state_out)) {
        quantum_release_state(state_out);
        return 0;
    }
    return 1;
}

static void quantum_release_state(QuantumStateGPU* state) {
    if (!state) { return; }
    if (state->buffer) {
        clReleaseMemObject(state->buffer);
        state->buffer = NULL;
    }
    state->num_qubits = 0;
    state->dimension = 0;
}

static int quantum_initialize_zero_state(QuantumStateGPU* state) {
    if (!state || !state->buffer) { return 0; }
    size_t bytes = state->dimension * sizeof(cl_float2);
    if (!zero_gpu_buffer(0, state->buffer, bytes)) {
        fprintf(stderr, "[C] Quantum: Failed to zero state buffer.\n");
        return 0;
    }
    cl_float2 init = make_complex(1.0f, 0.0f);
    cl_int err = clEnqueueWriteBuffer(queue, state->buffer, CL_TRUE, 0, sizeof(cl_float2), &init, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set |0...0> amplitude: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_initialize_basis_superposition(QuantumStateGPU* state, const uint32_t* basis_states, size_t count) {
    if (!state || !state->buffer || !basis_states || count == 0) { return 0; }
    size_t dimension = state->dimension;
    size_t bytes = dimension * sizeof(cl_float2);
    cl_float2* host_state = (cl_float2*)calloc(dimension, sizeof(cl_float2));
    if (!host_state) {
        fprintf(stderr, "[C] Quantum: Failed to allocate %zu bytes for custom state initialization.\n", bytes);
        return 0;
    }
    float amplitude = 1.0f / sqrtf((float)count);
    cl_float2 amp_complex = make_complex(amplitude, 0.0f);
    for (size_t i = 0; i < count; ++i) {
        uint32_t index = basis_states[i];
        if ((size_t)index >= dimension) {
            fprintf(stderr, "[C] Quantum: Basis index %u exceeds state dimension %zu.\n", index, dimension);
            free(host_state);
            return 0;
        }
        host_state[index] = amp_complex;
    }
    cl_int err = clEnqueueWriteBuffer(queue, state->buffer, CL_TRUE, 0, bytes, host_state, 0, NULL, NULL);
    free(host_state);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to upload custom superposition: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_measure_x_parity_gpu(QuantumStateGPU* state, const int* qubits, int count, float* out_value) {
    if (!state || !qubits || count <= 0 || !out_value) { return 0; }
    for (int i = 0; i < count; ++i) {
        if (qubits[i] < 0 || qubits[i] >= state->num_qubits) {
            fprintf(stderr, "[C] Quantum: Invalid qubit index %d for X-parity measurement.\n", qubits[i]);
            return 0;
        }
        if (!quantum_apply_hadamard(state, qubits[i])) {
            for (int j = i - 1; j >= 0; --j) { (void)quantum_apply_hadamard(state, qubits[j]); }
            return 0;
        }
    }
    uint64_t z_mask = 0;
    for (int i = 0; i < count; ++i) {
        z_mask |= ((uint64_t)1 << qubits[i]);
    }
    int ok = quantum_expectation_pauli_z_gpu(state, z_mask, out_value);
    for (int i = count - 1; i >= 0; --i) {
        if (!quantum_apply_hadamard(state, qubits[i])) {
            ok = 0;
        }
    }
    return ok;
}

static int quantum_prepare_steane_zero_state(QuantumStateGPU* state) {
    if (!state || state->num_qubits < 7) {
        fprintf(stderr, "[C] Quantum: Steane code requires at least 7 qubits (have %d).\n", state ? state->num_qubits : 0);
        return 0;
    }
    static const uint32_t steane_codewords[] = {
        0u,   15u,  51u,  60u,  85u,  90u, 102u, 105u
    };
    return quantum_initialize_basis_superposition(state, steane_codewords,
                                                  sizeof(steane_codewords) / sizeof(steane_codewords[0]));
}

static int quantum_apply_single_qubit_gate(QuantumStateGPU* state, int target,
                                           cl_float2 g00, cl_float2 g01, cl_float2 g10, cl_float2 g11) {
    if (!state || !state->buffer) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (target < 0 || target >= state->num_qubits) {
        fprintf(stderr, "[C] Quantum: Invalid target qubit %d for single qubit gate.\n", target);
        return 0;
    }
    if (state->dimension < 2) { return 1; }
    size_t global = state->dimension >> 1;
    if (global == 0) { return 1; }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_int), &target);
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_float2), &g00);
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_float2), &g01);
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_float2), &g10);
    err |= clSetKernelArg(quantum_single_qubit_kernel, arg++, sizeof(cl_float2), &g11);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set args for single qubit gate: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = ENQUEUE_KERNEL_PROFILED(quantum_single_qubit_kernel, 1, &global, NULL, "quantum_apply_single_qubit");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue single qubit gate: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after single qubit gate: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_apply_hadamard(QuantumStateGPU* state, int target) {
    const float inv_sqrt2 = 0.70710678118654752440f;
    return quantum_apply_single_qubit_gate(state, target,
                                           make_complex(inv_sqrt2, 0.0f),
                                           make_complex(inv_sqrt2, 0.0f),
                                           make_complex(inv_sqrt2, 0.0f),
                                           make_complex(-inv_sqrt2, 0.0f));
}

static int quantum_apply_pauli_x(QuantumStateGPU* state, int target) {
    return quantum_apply_single_qubit_gate(state, target,
                                           make_complex(0.0f, 0.0f),
                                           make_complex(1.0f, 0.0f),
                                           make_complex(1.0f, 0.0f),
                                           make_complex(0.0f, 0.0f));
}

static int quantum_apply_rotation_x(QuantumStateGPU* state, int target, float theta) {
    float theta_half = theta * 0.5f;
    float c = cosf(theta_half);
    float s = sinf(theta_half);
    return quantum_apply_single_qubit_gate(state, target,
                                           make_complex(c, 0.0f),
                                           make_complex(0.0f, -s),
                                           make_complex(0.0f, -s),
                                           make_complex(c, 0.0f));
}

static int quantum_apply_rotation_y(QuantumStateGPU* state, int target, float theta) {
    float theta_half = theta * 0.5f;
    float c = cosf(theta_half);
    float s = sinf(theta_half);
    return quantum_apply_single_qubit_gate(state, target,
                                           make_complex(c, 0.0f),
                                           make_complex(-s, 0.0f),
                                           make_complex(s, 0.0f),
                                           make_complex(c, 0.0f));
}

static int quantum_apply_pauli_y(QuantumStateGPU* state, int target) {
    /* Pauli-Y equals RY(pi) up to a global phase, which is sufficient here. */
    return quantum_apply_rotation_y(state, target, (float)M_PI);
}

static int quantum_apply_pauli_z(QuantumStateGPU* state, int target) {
    return quantum_apply_single_qubit_gate(state, target,
                                           make_complex(1.0f, 0.0f),
                                           make_complex(0.0f, 0.0f),
                                           make_complex(0.0f, 0.0f),
                                           make_complex(-1.0f, 0.0f));
}

static int quantum_apply_rotation_z(QuantumStateGPU* state, int target, float theta) {
    float theta_half = theta * 0.5f;
    cl_float2 g00 = make_complex(cosf(-theta_half), sinf(-theta_half));
    cl_float2 g11 = make_complex(cosf(theta_half), sinf(theta_half));
    return quantum_apply_single_qubit_gate(state, target,
                                           g00,
                                           make_complex(0.0f, 0.0f),
                                           make_complex(0.0f, 0.0f),
                                           g11);
}

static int quantum_apply_controlled_phase(QuantumStateGPU* state, int control, int target, float theta) {
    if (!state || !state->buffer) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (control < 0 || target < 0 || control >= state->num_qubits || target >= state->num_qubits) {
        fprintf(stderr, "[C] Quantum: Invalid qubit index for controlled phase (control=%d target=%d).\n", control, target);
        return 0;
    }
    cl_float2 phase = make_complex(cosf(theta), sinf(theta));
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_controlled_phase_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_controlled_phase_kernel, arg++, sizeof(cl_int), &control);
    err |= clSetKernelArg(quantum_controlled_phase_kernel, arg++, sizeof(cl_int), &target);
    err |= clSetKernelArg(quantum_controlled_phase_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    err |= clSetKernelArg(quantum_controlled_phase_kernel, arg++, sizeof(cl_float2), &phase);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set args for controlled phase: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = state->dimension;
    err = ENQUEUE_KERNEL_PROFILED(quantum_controlled_phase_kernel, 1, &global, NULL, "quantum_apply_controlled_phase");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue controlled phase: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after controlled phase: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_apply_controlled_not(QuantumStateGPU* state, int control, int target) {
    if (!state || !state->buffer) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (control < 0 || target < 0 || control >= state->num_qubits || target >= state->num_qubits) {
        fprintf(stderr, "[C] Quantum: Invalid qubit index for CNOT (control=%d target=%d).\n", control, target);
        return 0;
    }
    if (state->dimension < 2) { return 1; }
    size_t global = state->dimension >> 1;
    if (global == 0) { return 1; }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_controlled_not_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_controlled_not_kernel, arg++, sizeof(cl_int), &control);
    err |= clSetKernelArg(quantum_controlled_not_kernel, arg++, sizeof(cl_int), &target);
    err |= clSetKernelArg(quantum_controlled_not_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set args for controlled NOT: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = ENQUEUE_KERNEL_PROFILED(quantum_controlled_not_kernel, 1, &global, NULL, "quantum_apply_controlled_not");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue controlled NOT: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after controlled NOT: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_apply_swap_via_cnot(QuantumStateGPU* state, int q1, int q2) {
    if (q1 == q2) { return 1; }
    if (!quantum_apply_controlled_not(state, q1, q2)) { return 0; }
    if (!quantum_apply_controlled_not(state, q2, q1)) { return 0; }
    if (!quantum_apply_controlled_not(state, q1, q2)) { return 0; }
    return 1;
}

static int quantum_apply_controlled_rz_decomposed(QuantumStateGPU* state, int control, int target, float theta) {
    if (!quantum_apply_rotation_z(state, target, theta * 0.5f)) { return 0; }
    if (!quantum_apply_controlled_not(state, control, target)) { return 0; }
    if (!quantum_apply_rotation_z(state, target, -theta * 0.5f)) { return 0; }
    if (!quantum_apply_controlled_not(state, control, target)) { return 0; }
    return 1;
}

static int quantum_apply_controlled_rx_decomposed(QuantumStateGPU* state, int control, int target, float theta) {
    if (!quantum_apply_hadamard(state, target)) { return 0; }
    if (!quantum_apply_controlled_rz_decomposed(state, control, target, theta)) { return 0; }
    if (!quantum_apply_hadamard(state, target)) { return 0; }
    return 1;
}

static int quantum_apply_controlled_ry_decomposed(QuantumStateGPU* state, int control, int target, float theta) {
    const float half_pi = (float)(M_PI * 0.5);
    if (!quantum_apply_rotation_x(state, target, -half_pi)) { return 0; }
    if (!quantum_apply_controlled_rz_decomposed(state, control, target, theta)) { return 0; }
    if (!quantum_apply_rotation_x(state, target, half_pi)) { return 0; }
    return 1;
}

static int quantum_apply_toffoli_decomposed(QuantumStateGPU* state, int control1, int control2, int target) {
    const float pi_over_4 = (float)(M_PI * 0.25);
    if (!quantum_apply_hadamard(state, target)) { return 0; }
    if (!quantum_apply_controlled_not(state, control2, target)) { return 0; }
    if (!quantum_apply_rotation_z(state, target, -pi_over_4)) { return 0; }
    if (!quantum_apply_controlled_not(state, control1, target)) { return 0; }
    if (!quantum_apply_rotation_z(state, target, pi_over_4)) { return 0; }
    if (!quantum_apply_controlled_not(state, control2, target)) { return 0; }
    if (!quantum_apply_rotation_z(state, target, -pi_over_4)) { return 0; }
    if (!quantum_apply_controlled_not(state, control1, target)) { return 0; }
    if (!quantum_apply_rotation_z(state, control2, pi_over_4)) { return 0; }
    if (!quantum_apply_rotation_z(state, target, pi_over_4)) { return 0; }
    if (!quantum_apply_hadamard(state, target)) { return 0; }
    if (!quantum_apply_controlled_not(state, control1, control2)) { return 0; }
    if (!quantum_apply_rotation_z(state, control1, pi_over_4)) { return 0; }
    if (!quantum_apply_rotation_z(state, control2, -pi_over_4)) { return 0; }
    if (!quantum_apply_controlled_not(state, control1, control2)) { return 0; }
    return 1;
}

#ifndef NDEBUG
static int quantum_check_norm1(int gpu_index, QuantumStateGPU* state, float eps, const char* stage) {
    if (!state || !state->buffer) { return 0; }
    if (state->dimension == 0) { return 1; }
    size_t bytes = state->dimension * sizeof(cl_float2);
    cl_float2* host = (cl_float2*)malloc(bytes);
    if (!host) {
        fprintf(stderr, "[C] Quantum Echoes: DEBUG norm check allocation failed for %zu bytes.\n", bytes);
        return 0;
    }
    cl_command_queue active_queue = queue;
    GpuSlot* slot = cc_get_slot(gpu_index);
    if (slot && slot->queue) {
        active_queue = slot->queue;
    }
    cl_int err = clEnqueueReadBuffer(active_queue, state->buffer, CL_TRUE, 0, bytes, host, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum Echoes: DEBUG norm check read failed: %s (%d).\n", clGetErrorString(err), err);
        free(host);
        return 0;
    }
    double norm = 0.0;
    for (size_t i = 0; i < state->dimension; ++i) {
        double re = host[i].s[0];
        double im = host[i].s[1];
        norm += re * re + im * im;
    }
    free(host);
    double deviation = fabs(norm - 1.0);
    if (deviation > eps) {
        fprintf(stderr, "[C] Quantum Echoes: WARN Norm deviation at %s: |psi|^2 = %.6f (tol %.6f)\n",
                stage ? stage : "<unnamed>", norm, eps);
    }
    return 1;
}
#endif

/**
 * @brief Dispatch a QuantumGate descriptor to the corresponding GPU gate routine.
 *
 * @param state Quantum state to mutate.
 * @param gate  Descriptor describing the gate operation to apply.
 *
 * @return 1 on success, 0 on failure (invalid arguments or unsupported gate).
 */
static int quantum_apply_gate_from_desc(QuantumStateGPU* state, const QuantumGate* gate) {
    if (!state || !gate) {
        fprintf(stderr, "[C] Quantum: Invalid arguments to quantum_apply_gate_from_desc (state=%p gate=%p).\n",
                (void*)state, (const void*)gate);
        return 0;
    }
    int result = 0;
    int gate_arity = gate->arity;
    if (strncmp(gate->name, "H", sizeof(gate->name)) == 0) {
        result = quantum_apply_hadamard(state, (int)gate->target);
        gate_arity = 1;
    } else if (strncmp(gate->name, "X", sizeof(gate->name)) == 0) {
        result = quantum_apply_pauli_x(state, (int)gate->target);
        gate_arity = 1;
    } else if (strncmp(gate->name, "Y", sizeof(gate->name)) == 0) {
        result = quantum_apply_pauli_y(state, (int)gate->target);
        gate_arity = 1;
    } else if (strncmp(gate->name, "Z", sizeof(gate->name)) == 0) {
        result = quantum_apply_pauli_z(state, (int)gate->target);
        gate_arity = 1;
    } else if (strncmp(gate->name, "RX", sizeof(gate->name)) == 0) {
        result = quantum_apply_rotation_x(state, (int)gate->target, gate->params[0]);
        gate_arity = 1;
    } else if (strncmp(gate->name, "RY", sizeof(gate->name)) == 0) {
        result = quantum_apply_rotation_y(state, (int)gate->target, gate->params[0]);
        gate_arity = 1;
    } else if (strncmp(gate->name, "RZ", sizeof(gate->name)) == 0) {
        result = quantum_apply_rotation_z(state, (int)gate->target, gate->params[0]);
        gate_arity = 1;
    } else if (strncmp(gate->name, "CNOT", sizeof(gate->name)) == 0) {
        result = quantum_apply_controlled_not(state, (int)gate->control, (int)gate->target);
        gate_arity = 2;
    } else if (strncmp(gate->name, "CPHASE", sizeof(gate->name)) == 0) {
        result = quantum_apply_controlled_phase(state, (int)gate->control, (int)gate->target, gate->params[0]);
        gate_arity = 2;
    } else if (strncmp(gate->name, "SWAP", sizeof(gate->name)) == 0) {
        result = quantum_apply_swap_via_cnot(state, (int)gate->control, (int)gate->target);
        gate_arity = 2;
    } else if (strncmp(gate->name, "CCX", sizeof(gate->name)) == 0 ||
               strncmp(gate->name, "TOFF", 5) == 0) {
        result = quantum_apply_toffoli_decomposed(state, (int)gate->control, (int)gate->control2, (int)gate->target);
        gate_arity = 3;
    } else if (strncmp(gate->name, "CRZ", sizeof(gate->name)) == 0) {
        result = quantum_apply_controlled_rz_decomposed(state, (int)gate->control, (int)gate->target, gate->params[0]);
        gate_arity = 2;
    } else if (strncmp(gate->name, "CRX", sizeof(gate->name)) == 0) {
        result = quantum_apply_controlled_rx_decomposed(state, (int)gate->control, (int)gate->target, gate->params[0]);
        gate_arity = 2;
    } else if (strncmp(gate->name, "CRY", sizeof(gate->name)) == 0) {
        result = quantum_apply_controlled_ry_decomposed(state, (int)gate->control, (int)gate->target, gate->params[0]);
        gate_arity = 2;
    } else {
        fprintf(stderr,
                "[C] Quantum: Unsupported gate '%s' (arity=%d control=%d control2=%d target=%d) in descriptor dispatch.\n",
                gate->name, gate->arity, gate->control, gate->control2, gate->target);
        return 0;
    }

    if (result && g_active_quantum_profile) {
        g_active_quantum_profile->total_gate_applications++;
        g_active_quantum_profile->kernel_enqueue_count++;
        if (gate_arity <= 1) {
            g_active_quantum_profile->single_qubit_gate_count++;
        } else if (gate_arity == 2) {
            g_active_quantum_profile->two_qubit_gate_count++;
        } else {
            g_active_quantum_profile->three_qubit_gate_count++;
        }
        if (state) {
            uint64_t bytes = (uint64_t)state->dimension * sizeof(cl_float2);
            g_active_quantum_profile->estimated_global_mem_bytes += bytes;
        }
    }
    return result;
}

static void quantum_profile_record_fused_group(void) {
    if (g_active_quantum_profile) {
        g_active_quantum_profile->fused_single_gate_groups++;
    }
}

static int quantum_apply_sequence(QuantumStateGPU* state, const QuantumGate* seq, int count) {
    if (!seq && count > 0) {
        fprintf(stderr, "[C] Quantum: Gate sequence pointer is NULL.\n");
        return 0;
    }
    for (int i = 0; i < count; ++i) {
        const QuantumGate* gate = &seq[i];
        if (gate->arity == 1) {
            if (strncmp(gate->name, "RX", sizeof(gate->name)) == 0 ||
                strncmp(gate->name, "RY", sizeof(gate->name)) == 0 ||
                strncmp(gate->name, "RZ", sizeof(gate->name)) == 0) {
                QuantumGate fused = *gate;
                int j = i + 1;
                while (j < count && seq[j].arity == 1 &&
                       strncmp(seq[j].name, gate->name, sizeof(gate->name)) == 0 &&
                       seq[j].target == gate->target) {
                    fused.params[0] += seq[j].params[0];
                    ++j;
                }
                if (!quantum_apply_gate_from_desc(state, &fused)) {
                    return 0;
                }
                if (j - i > 1) {
                    quantum_profile_record_fused_group();
                }
                i = j - 1;
                continue;
            } else if (strncmp(gate->name, "X", sizeof(gate->name)) == 0 ||
                       strncmp(gate->name, "Z", sizeof(gate->name)) == 0 ||
                       strncmp(gate->name, "Y", sizeof(gate->name)) == 0) {
                int parity = 1;
                int j = i + 1;
                while (j < count && seq[j].arity == 1 &&
                       strncmp(seq[j].name, gate->name, sizeof(gate->name)) == 0 &&
                       seq[j].target == gate->target) {
                    parity ^= 1;
                    ++j;
                }
                if (parity) {
                    if (!quantum_apply_gate_from_desc(state, gate)) {
                        return 0;
                    }
                } else {
                    quantum_profile_record_fused_group();
                }
                i = j - 1;
                continue;
            }
        }
        if (!quantum_apply_gate_from_desc(state, gate)) {
            return 0;
        }
    }
    return 1;
}

/**
 * @brief Apply the adjoint of a gate sequence in reverse order.
 *
 * @param state Quantum state to operate on.
 * @param seq   Gate descriptor list representing the original forward sequence.
 * @param count Number of gates contained in @p seq.
 *
 * @return 1 on success, 0 if any gate application fails or invalid arguments are provided.
 */
static int quantum_apply_sequence_dagger(QuantumStateGPU* state, const QuantumGate* seq, int count) {
    if (!seq && count > 0) {
        fprintf(stderr, "[C] Quantum: Gate sequence pointer is NULL for dagger application.\n");
        return 0;
    }
    for (int i = count - 1; i >= 0; --i) {
        QuantumGate gate = seq[i];
        if (strncmp(gate.name, "RX", sizeof(gate.name)) == 0 ||
            strncmp(gate.name, "RY", sizeof(gate.name)) == 0 ||
            strncmp(gate.name, "RZ", sizeof(gate.name)) == 0 ||
            strncmp(gate.name, "CPHASE", sizeof(gate.name)) == 0 ||
            strncmp(gate.name, "CRX", sizeof(gate.name)) == 0 ||
            strncmp(gate.name, "CRY", sizeof(gate.name)) == 0 ||
            strncmp(gate.name, "CRZ", sizeof(gate.name)) == 0) {
            gate.params[0] = -gate.params[0];
        }
        if (!quantum_apply_gate_from_desc(state, &gate)) {
            return 0;
        }
    }
    return 1;
}

/**
 * @brief Apply the adjoint of a single gate descriptor.
 *
 * @param state Quantum state to operate on.
 * @param gate  Gate descriptor to adjoint-apply (must not be NULL).
 *
 * @return 1 on success, 0 on failure (e.g., unsupported gate or invalid arguments).
 */
static int quantum_apply_gate_dagger(QuantumStateGPU* state, const QuantumGate* gate) {
    if (!gate) {
        fprintf(stderr, "[C] Quantum: Gate pointer is NULL for dagger application.\n");
        return 0;
    }
    QuantumGate adj_gate = *gate;
    if (strncmp(adj_gate.name, "RX", sizeof(adj_gate.name)) == 0 ||
        strncmp(adj_gate.name, "RY", sizeof(adj_gate.name)) == 0 ||
        strncmp(adj_gate.name, "RZ", sizeof(adj_gate.name)) == 0 ||
        strncmp(adj_gate.name, "CPHASE", sizeof(adj_gate.name)) == 0 ||
        strncmp(adj_gate.name, "CRX", sizeof(adj_gate.name)) == 0 ||
        strncmp(adj_gate.name, "CRY", sizeof(adj_gate.name)) == 0 ||
        strncmp(adj_gate.name, "CRZ", sizeof(adj_gate.name)) == 0) {
        adj_gate.params[0] = -adj_gate.params[0];
    }
    return quantum_apply_gate_from_desc(state, &adj_gate);
}

static int quantum_swap_qubits_out_of_place(QuantumStateGPU* state, int q1, int q2) {
    if (!state || !state->buffer) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (q1 < 0 || q2 < 0 || q1 >= state->num_qubits || q2 >= state->num_qubits || q1 == q2) {
        return 1;
    }
    if (!quantum_reserve_temp_state(state->dimension)) { return 0; }
    if (!zero_gpu_buffer(0, quantum_temp_state_buffer, state->dimension * sizeof(cl_float2))) {
        fprintf(stderr, "[C] Quantum: Failed to zero temp buffer for swap.\n");
        return 0;
    }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_swap_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_swap_kernel, arg++, sizeof(cl_mem), &quantum_temp_state_buffer);
    err |= clSetKernelArg(quantum_swap_kernel, arg++, sizeof(cl_int), &q1);
    err |= clSetKernelArg(quantum_swap_kernel, arg++, sizeof(cl_int), &q2);
    err |= clSetKernelArg(quantum_swap_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set args for swap kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = state->dimension;
    err = ENQUEUE_KERNEL_PROFILED(quantum_swap_kernel, 1, &global, NULL, "quantum_swap_qubits");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue swap kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after swap kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t bytes = state->dimension * sizeof(cl_float2);
    err = clEnqueueCopyBuffer(queue, quantum_temp_state_buffer, state->buffer, 0, 0, bytes, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to copy swapped state back: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after swap copy: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_inverse_qft(QuantumStateGPU* state, int start_qubit, int count) {
    if (count <= 0) { return 1; }
    for (int q = start_qubit + count - 1; q >= start_qubit; --q) {
        for (int m = q - 1; m >= start_qubit; --m) {
            float angle = -((float)M_PI) / (float)(1 << (q - m));
            if (!quantum_apply_controlled_phase(state, m, q, angle)) { return 0; }
        }
        if (!quantum_apply_hadamard(state, q)) { return 0; }
    }
    for (int i = 0; i < count / 2; ++i) {
        if (!quantum_swap_qubits_out_of_place(state, start_qubit + i, start_qubit + count - 1 - i)) {
            return 0;
        }
    }
    return 1;
}

static int quantum_apply_modular_exponentiation(QuantumStateGPU* state, int num_control, int num_work, int base_a, int modulus_N) {
    if (!state || !state->buffer) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (num_control < 1 || num_work < 1 || num_control + num_work != state->num_qubits) {
        fprintf(stderr, "[C] Quantum: Invalid register partition (control=%d work=%d total=%d).\n",
                num_control, num_work, state->num_qubits);
        return 0;
    }
    if (!quantum_reserve_temp_state(state->dimension)) { return 0; }
    if (!zero_gpu_buffer(0, quantum_temp_state_buffer, state->dimension * sizeof(cl_float2))) {
        fprintf(stderr, "[C] Quantum: Failed to zero temp buffer for modular exponentiation.\n");
        return 0;
    }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_modexp_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_modexp_kernel, arg++, sizeof(cl_mem), &quantum_temp_state_buffer);
    err |= clSetKernelArg(quantum_modexp_kernel, arg++, sizeof(cl_int), &num_control);
    err |= clSetKernelArg(quantum_modexp_kernel, arg++, sizeof(cl_int), &num_work);
    err |= clSetKernelArg(quantum_modexp_kernel, arg++, sizeof(cl_int), &base_a);
    err |= clSetKernelArg(quantum_modexp_kernel, arg++, sizeof(cl_int), &modulus_N);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set args for modular exponentiation: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = state->dimension;
    err = ENQUEUE_KERNEL_PROFILED(quantum_modexp_kernel, 1, &global, NULL, "quantum_modular_exponentiation");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue modular exponentiation: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after modular exponentiation: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t bytes = state->dimension * sizeof(cl_float2);
    err = clEnqueueCopyBuffer(queue, quantum_temp_state_buffer, state->buffer, 0, 0, bytes, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to copy modular exponentiation result: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after modular exponentiation copy: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_prepare_uniform_superposition(QuantumStateGPU* state, int num_qubits_to_prepare, int start_qubit) {
    for (int i = 0; i < num_qubits_to_prepare; ++i) {
        if (!quantum_apply_hadamard(state, start_qubit + i)) {
            return 0;
        }
    }
    return 1;
}

static int quantum_apply_grover_oracle(QuantumStateGPU* state, uint64_t mask, uint64_t value) {
    if (!state || !state->buffer) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_phase_oracle_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_phase_oracle_kernel, arg++, sizeof(cl_ulong), &mask);
    err |= clSetKernelArg(quantum_phase_oracle_kernel, arg++, sizeof(cl_ulong), &value);
    err |= clSetKernelArg(quantum_phase_oracle_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set oracle args: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = state->dimension;
    err = ENQUEUE_KERNEL_PROFILED(quantum_phase_oracle_kernel, 1, &global, NULL, "quantum_phase_oracle");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue oracle kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after oracle: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

static int quantum_apply_grover_diffusion(QuantumStateGPU* state) {
    if (!quantum_prepare_uniform_superposition(state, state->num_qubits, 0)) { return 0; }
    size_t dimension = state->dimension;
    if (dimension > (size_t)UINT_MAX) {
        fprintf(stderr, "[C] Quantum: Dimension %zu exceeds cl_uint range for phase-zero kernel.\n", dimension);
        return 0;
    }
    cl_uint dimension_uint = (cl_uint)dimension;
    cl_int err = CL_SUCCESS;
    err |= clSetKernelArg(quantum_phase_zero_kernel, 0, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_phase_zero_kernel, 1, sizeof(cl_uint), &dimension_uint);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set phase-zero args: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = ENQUEUE_KERNEL_PROFILED(quantum_phase_zero_kernel, 1, &dimension, NULL, "quantum_phase_flip_except_zero");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue phase-zero kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after phase-zero: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    if (!quantum_prepare_uniform_superposition(state, state->num_qubits, 0)) { return 0; }
    return 1;
}

static int quantum_compute_probabilities_gpu(QuantumStateGPU* state, cl_mem* probs_out) {
    if (!state || !state->buffer || !probs_out) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (!quantum_reserve_probability_buffer(state->dimension)) { return 0; }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_probability_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_probability_kernel, arg++, sizeof(cl_mem), &quantum_probability_buffer);
    err |= clSetKernelArg(quantum_probability_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set probability kernel args: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = state->dimension;
    err = ENQUEUE_KERNEL_PROFILED(quantum_probability_kernel, 1, &global, NULL, "quantum_compute_probabilities");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue probability kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after probability kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    *probs_out = quantum_probability_buffer;
    return 1;
}

static int quantum_expectation_pauli_z_gpu(QuantumStateGPU* state, uint64_t z_mask, float* out_value) {
    if (!state || !out_value) { return 0; }
    if (!ensure_quantum_kernels_ready()) { return 0; }
    if (!quantum_reserve_probability_buffer(state->dimension)) { return 0; }
    cl_int err = CL_SUCCESS;
    int arg = 0;
    err |= clSetKernelArg(quantum_expectation_pauli_z_kernel, arg++, sizeof(cl_mem), &state->buffer);
    err |= clSetKernelArg(quantum_expectation_pauli_z_kernel, arg++, sizeof(cl_mem), &quantum_probability_buffer);
    err |= clSetKernelArg(quantum_expectation_pauli_z_kernel, arg++, sizeof(cl_int), &state->num_qubits);
    err |= clSetKernelArg(quantum_expectation_pauli_z_kernel, arg++, sizeof(cl_ulong), &z_mask);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to set expectation kernel args: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t global = state->dimension;
    err = ENQUEUE_KERNEL_PROFILED(quantum_expectation_pauli_z_kernel, 1, &global, NULL, "quantum_expectation_pauli_z");
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to enqueue expectation kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    err = clFinish(queue);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: clFinish failed after expectation kernel: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }
    size_t bytes = state->dimension * sizeof(cl_float);
    float* host_terms = (float*)malloc(bytes);
    if (!host_terms) {
        fprintf(stderr, "[C] Quantum: Failed to allocate host buffer for expectation (size=%zu).\n", bytes);
        return 0;
    }
    err = clEnqueueReadBuffer(queue, quantum_probability_buffer, CL_TRUE, 0, bytes, host_terms, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to read expectation buffer: %s (%d)\n", clGetErrorString(err), err);
        free(host_terms);
        return 0;
    }
    float accum = 0.0f;
    for (size_t i = 0; i < state->dimension; ++i) {
        accum += host_terms[i];
    }
    free(host_terms);
    *out_value = accum;
    return 1;
}

static int quantum_measure_most_probable(QuantumStateGPU* state, int* out_index) {
    if (!out_index) { return 0; }
    cl_mem probs = NULL;
    if (!quantum_compute_probabilities_gpu(state, &probs)) { return 0; }
    size_t bytes = state->dimension * sizeof(cl_float);
    float* host_probs = (float*)malloc(bytes);
    if (!host_probs) {
        fprintf(stderr, "[C] Quantum: Failed to allocate host probabilities (size=%zu).\n", bytes);
        return 0;
    }
    cl_int err = clEnqueueReadBuffer(queue, probs, CL_TRUE, 0, bytes, host_probs, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] Quantum: Failed to read probabilities: %s (%d)\n", clGetErrorString(err), err);
        free(host_probs);
        return 0;
    }
    int best_index = 0;
    float best_value = -1.0f;
    for (size_t i = 0; i < state->dimension; ++i) {
        if (host_probs[i] > best_value) {
            best_value = host_probs[i];
            best_index = (int)i;
        }
    }
    free(host_probs);
    *out_index = best_index;
    return 1;
}

static int quantum_prepare_feature_map(QuantumStateGPU* state, const float* feature_vector, int num_features) {
    if (!feature_vector || num_features <= 0) { return 0; }
    for (int q = 0; q < state->num_qubits; ++q) {
        float feature = feature_vector[q % num_features];
        if (!quantum_apply_rotation_y(state, q, feature)) { return 0; }
        if (!quantum_apply_rotation_z(state, q, feature * 0.5f)) { return 0; }
    }
    return 1;
}

static int quantum_apply_qml_classifier_layer(QuantumStateGPU* state, const float* parameters, int num_qubits) {
    if (!parameters) { return 0; }
    for (int q = 0; q < num_qubits; ++q) {
        float theta = parameters[q];
        if (!quantum_apply_rotation_x(state, q, theta)) { return 0; }
        if (!quantum_apply_rotation_z(state, q, theta * 0.5f)) { return 0; }
    }
    for (int q = 0; q < num_qubits - 1; ++q) {
        if (!quantum_apply_controlled_not(state, q, q + 1)) { return 0; }
    }
    return 1;
}

static uint32_t round_up_to_power_of_two(uint32_t value) {
    if (value == 0) { return 1; }
    value--;
    value |= value >> 1;
    value |= value >> 2;
    value |= value >> 4;
    value |= value >> 8;
    value |= value >> 16;
    value++;
    return value;
}

static uint64_t host_modexp_uint64(uint64_t base, uint64_t exp, uint64_t mod) {
    if (mod == 1) { return 0; }
    uint64_t result = 1 % mod;
    uint64_t b = base % mod;
    while (exp > 0) {
        if (exp & 1ULL) {
            result = (result * b) % mod;
        }
        b = (b * b) % mod;
        exp >>= 1ULL;
    }
    return result;
}

static int quantum_apply_vqe_ansatz(QuantumStateGPU* state, int num_qubits, int ansatz_layers, const float* parameters, int num_parameters) {
    if (!state || !state->buffer || !parameters) { return 0; }
    int params_per_layer = 2 * num_qubits;
    if (ansatz_layers <= 0 || num_parameters < ansatz_layers * params_per_layer) {
        fprintf(stderr, "[C] VQE: Parameter vector too small (have %d need %d).\n",
                num_parameters, ansatz_layers * params_per_layer);
        return 0;
    }
    if (!quantum_initialize_zero_state(state)) { return 0; }
    for (int layer = 0; layer < ansatz_layers; ++layer) {
        const float* layer_params = parameters + layer * params_per_layer;
        for (int q = 0; q < num_qubits; ++q) {
            float theta_y = layer_params[q];
            float theta_z = layer_params[q + num_qubits];
            if (!quantum_apply_rotation_y(state, q, theta_y)) { return 0; }
            if (!quantum_apply_rotation_z(state, q, theta_z)) { return 0; }
        }
        for (int q = 0; q < num_qubits - 1; ++q) {
            if (!quantum_apply_controlled_not(state, q, q + 1)) { return 0; }
        }
        if (num_qubits > 1) {
            if (!quantum_apply_controlled_not(state, num_qubits - 1, 0)) { return 0; }
        }
    }
    return 1;
}

static int quantum_compute_pauli_z_energy(QuantumStateGPU* state, const PauliZTerm* terms, int num_terms, float* out_energy) {
    if (!out_energy) { return 0; }
    float energy = 0.0f;
    for (int i = 0; i < num_terms; ++i) {
        float expectation = 0.0f;
        if (!quantum_expectation_pauli_z_gpu(state, terms[i].z_mask, &expectation)) {
            return 0;
        }
        energy += terms[i].coefficient * expectation;
    }
    *out_energy = energy;
    return 1;
}

static int quantum_apply_multi_qubit_z_phase(QuantumStateGPU* state, uint64_t mask, float angle) {
    if (mask == 0) { return 1; }
    int qubits[64];
    int count = 0;
    for (int q = 0; q < state->num_qubits; ++q) {
        if (mask & (1ULL << q)) {
            qubits[count++] = q;
        }
    }
    if (count == 0) { return 1; }
    if (count == 1) {
        return quantum_apply_rotation_z(state, qubits[0], 2.0f * angle);
    }
    int target = qubits[count - 1];
    for (int i = 0; i < count - 1; ++i) {
        if (!quantum_apply_controlled_not(state, qubits[i], target)) { return 0; }
    }
    if (!quantum_apply_rotation_z(state, target, 2.0f * angle)) { return 0; }
    for (int i = count - 2; i >= 0; --i) {
        if (!quantum_apply_controlled_not(state, qubits[i], target)) { return 0; }
    }
    return 1;
}

static int solve_linear_system(const float* matrix, const float* vector, int n, float* solution) {
    if (!matrix || !vector || !solution || n <= 0) { return 0; }
    float* augmented = (float*)malloc(n * (n + 1) * sizeof(float));
    if (!augmented) { return 0; }
    for (int i = 0; i < n; ++i) {
        for (int j = 0; j < n; ++j) {
            augmented[i * (n + 1) + j] = matrix[i * n + j];
        }
        augmented[i * (n + 1) + n] = vector[i];
    }

    for (int col = 0; col < n; ++col) {
        int pivot = col;
        float max_val = fabsf(augmented[pivot * (n + 1) + col]);
        for (int row = col + 1; row < n; ++row) {
            float val = fabsf(augmented[row * (n + 1) + col]);
            if (val > max_val) { pivot = row; max_val = val; }
        }
        if (max_val < 1e-8f) {
            free(augmented);
            return 0;
        }
        if (pivot != col) {
            for (int k = col; k <= n; ++k) {
                float tmp = augmented[col * (n + 1) + k];
                augmented[col * (n + 1) + k] = augmented[pivot * (n + 1) + k];
                augmented[pivot * (n + 1) + k] = tmp;
            }
        }
        float pivot_val = augmented[col * (n + 1) + col];
        for (int k = col; k <= n; ++k) {
            augmented[col * (n + 1) + k] /= pivot_val;
        }
        for (int row = 0; row < n; ++row) {
            if (row == col) { continue; }
            float factor = augmented[row * (n + 1) + col];
            for (int k = col; k <= n; ++k) {
                augmented[row * (n + 1) + k] -= factor * augmented[col * (n + 1) + k];
            }
        }
    }

    for (int i = 0; i < n; ++i) {
        solution[i] = augmented[i * (n + 1) + n];
    }
    free(augmented);
    return 1;
}

/**
 * @brief Submits a command to the OpenCL command queue for execution.
 */
int submit_kernel_command(int gpu_index, GPUCommand command, void *data) {
    cl_int err = CL_SUCCESS;
    cc_clear_last_error();
    cl_command_queue active_queue = queue;
    if (!active_queue) {
        cc_set_last_error("[C] submit_kernel_command: Error - Invalid command queue (NULL)");
        fprintf(stderr, "[C] submit_kernel_command: Error - Invalid command queue (NULL).\n");
        return 0;
    }
    g_thread_queue = active_queue;
    g_thread_gpu_index = gpu_index;

    #define CHECK_CL_ERR(call, kernel_name_str) \
        err = (call); \
        if (err != CL_SUCCESS) { \
            cc_set_last_error("[C] OpenCL Error (%s): %s (%d) during '%s'", \
                              kernel_name_str, clGetErrorString(err), err, #call); \
            fprintf(stderr, "[C] OpenCL Error (%s): %s (%d) during '%s' in %s line %d\n", \
                    kernel_name_str, clGetErrorString(err), err, #call, __FILE__, __LINE__); \
            return 0; \
        }

    size_t lws_reduce; size_t local_mem_bytes;

    switch(command) {
        // --- Standard Kernels ---
        case COMMAND_MATRIX_MULTIPLY: {
            BMMCommandData* cmd = (BMMCommandData*)data;
            if ((!matmul_kernel && !matmul_kernel_fast) || !cmd || !cmd->buffer_a || !cmd->buffer_b || !cmd->buffer_c) { fprintf(stderr, "[C] Submit MatMul: Invalid args or kernel.\n"); return 0;}
            if (cmd->B <= 0 || cmd->M <= 0 || cmd->N <= 0) { if ((size_t)cmd->B * cmd->M * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit MatMul: Invalid dimensions B/M/N.\n"); return 0; }
            if (cmd->K <= 0) { fprintf(stderr, "[C] Submit MatMul: Invalid dimension K.\n"); return 0;}
            cl_kernel kernel = matmul_kernel_fast ? matmul_kernel_fast : matmul_kernel;
            cl_mem a = (cl_mem)cmd->buffer_a, b = (cl_mem)cmd->buffer_b, c = (cl_mem)cmd->buffer_c;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &a), "BMM Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &b), "BMM Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &c), "BMM Fwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->B), "BMM Fwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->M), "BMM Fwd Arg 4");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->N), "BMM Fwd Arg 5");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_int), &cmd->K), "BMM Fwd Arg 6");
            size_t gws[3] = { (size_t)cmd->N, (size_t)cmd->M, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 3, gws, NULL, "matmul_forward"), "BMM Fwd Enqueue");
            return 1;
        }
        case COMMAND_SOFTMAX_ROWWISE: {
            SoftmaxCommandData* cmd = (SoftmaxCommandData*)data;
            if ((!softmax_kernel && !softmax_kernel_fast) || !cmd || !cmd->buffer_input || !cmd->buffer_output) { fprintf(stderr, "[C] Submit Softmax: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_rows <= 0 || cmd->row_size <= 0) { if (cmd->num_rows == 0) return 1; fprintf(stderr, "[C] Submit Softmax: Invalid dimensions.\n"); return 0; }
            cl_kernel kernel = softmax_kernel ? softmax_kernel : softmax_kernel_fast; /* prefer strict for accuracy */
            cl_mem in = (cl_mem)cmd->buffer_input, out = (cl_mem)cmd->buffer_output;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in), "Softmax Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &out), "Softmax Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->num_rows), "Softmax Fwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->row_size), "Softmax Fwd Arg 3");
            size_t workgroup = (cmd->row_size >= 256) ? 256 : 128;
            size_t scratch_bytes = workgroup * sizeof(float);
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, scratch_bytes, NULL), "Softmax Fwd Arg 4 (scratch max)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, scratch_bytes, NULL), "Softmax Fwd Arg 5 (scratch sum)");
            size_t gws[1] = { (size_t)cmd->num_rows * workgroup };
            size_t lws[1] = { workgroup };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, lws, "softmax_rowwise"), "Softmax Fwd Enqueue");
            return 1;
        }
        case COMMAND_GELU_ELEMENTWISE: {
            GeluCommandData* cmd = (GeluCommandData*)data;
            if ((!gelu_kernel && !gelu_kernel_fast) || !cmd || !cmd->buffer_input || !cmd->buffer_output) { fprintf(stderr, "[C] Submit GELU: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit GELU: Invalid dimensions.\n"); return 0; }
            cl_kernel kernel = gelu_kernel_fast ? gelu_kernel_fast : gelu_kernel;
            cl_mem in = (cl_mem)cmd->buffer_input, out = (cl_mem)cmd->buffer_output;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in), "GELU Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &out), "GELU Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->num_elements), "GELU Fwd Arg 2");
            size_t gws[1] = { (size_t)cmd->num_elements };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "gelu_forward"), "GELU Fwd Enqueue");
            return 1;
        }
        case COMMAND_ADD_ELEMENTWISE: {
             AddCommandData* cmd = (AddCommandData*)data;
             if ((!add_kernel && !add_kernel_fast) || !cmd || !cmd->buffer_a || !cmd->buffer_b || !cmd->buffer_c) { fprintf(stderr, "[C] Submit Add: Invalid args or kernel.\n"); return 0; }
             if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit Add: Invalid dimensions.\n"); return 0; }
             cl_kernel kernel = add_kernel_fast ? add_kernel_fast : add_kernel;
             cl_mem a = (cl_mem)cmd->buffer_a, b = (cl_mem)cmd->buffer_b, c = (cl_mem)cmd->buffer_c;
             CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &a), "Add Fwd Arg 0");
             CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &b), "Add Fwd Arg 1");
             CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &c), "Add Fwd Arg 2");
             CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->num_elements), "Add Fwd Arg 3");
             size_t gws[1] = { (size_t)cmd->num_elements };
             CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "add_forward"), "Add Fwd Enqueue");
             return 1;
        }
        case COMMAND_MUL_ELEMENTWISE: {
            MulCommandData* cmd = (MulCommandData*)data;
            if ((!mul_kernel && !mul_kernel_fast) || !cmd || !cmd->buffer_a || !cmd->buffer_b || !cmd->buffer_c) { fprintf(stderr, "[C] Submit Mul: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit Mul: Invalid dimensions.\n"); return 0; }
            cl_kernel kernel = mul_kernel_fast ? mul_kernel_fast : mul_kernel;
            cl_mem a = (cl_mem)cmd->buffer_a, b = (cl_mem)cmd->buffer_b, c = (cl_mem)cmd->buffer_c;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &a), "Mul Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &b), "Mul Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &c), "Mul Fwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->num_elements), "Mul Fwd Arg 3");
            size_t gws[1] = { (size_t)cmd->num_elements };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "mul_forward"), "Mul Fwd Enqueue");
            return 1;
        }
        case COMMAND_LAYER_NORM: {
            LayerNormCommandData* cmd = (LayerNormCommandData*)data;
            if (!layernorm_kernel || !cmd || !cmd->buffer_input || !cmd->buffer_output) { fprintf(stderr, "[C] Submit LayerNorm: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_rows <= 0 || cmd->row_size <= 0) { if (cmd->num_rows == 0) return 1; fprintf(stderr, "[C] Submit LayerNorm: Invalid dimensions.\n"); return 0; }
            cl_mem in = (cl_mem)cmd->buffer_input, out = (cl_mem)cmd->buffer_output;
            float effective_eps = (cmd->eps > 0) ? cmd->eps : 1e-5f;
            CHECK_CL_ERR(clSetKernelArg(layernorm_kernel, 0, sizeof(cl_mem), &in), "LayerNorm Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(layernorm_kernel, 1, sizeof(cl_mem), &out), "LayerNorm Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(layernorm_kernel, 2, sizeof(cl_int), &cmd->num_rows), "LayerNorm Fwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(layernorm_kernel, 3, sizeof(cl_int), &cmd->row_size), "LayerNorm Fwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(layernorm_kernel, 4, sizeof(cl_float), &effective_eps), "LayerNorm Fwd Arg 4");
            size_t gws[1] = { (size_t)cmd->num_rows };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(layernorm_kernel, 1, gws, NULL, "layernorm_forward"), "LayerNorm Fwd Enqueue");
            return 1;
        }
        case COMMAND_CLONE: {
            CloneCommandData* cmd = (CloneCommandData*)data;
            if (!cmd || !cmd->src_buffer || !cmd->dst_buffer) { fprintf(stderr, "[C] Submit Clone: Invalid args.\n"); return 0; }
            if (cmd->size == 0) return 1;
            cl_mem src = (cl_mem)cmd->src_buffer;
            cl_mem dst = (cl_mem)cmd->dst_buffer;
            CHECK_CL_ERR(clEnqueueCopyBuffer(active_queue, src, dst, 0, 0, cmd->size, 0, NULL, NULL), "Clone Enqueue (CopyBuffer)");
            return 1;
        }
        case COMMAND_TRANSPOSE: {
            TransposeCommandData* cmd = (TransposeCommandData*)data;
            if ((!transpose_kernel && !transpose_kernel_fast) || !cmd || !cmd->buffer_input || !cmd->buffer_output) { fprintf(stderr, "[C] Submit Transpose2D: Invalid args or kernel.\n"); return 0; }
            if (cmd->rows <= 0 || cmd->cols <= 0) { if ((size_t)cmd->rows * cmd->cols == 0) return 1; fprintf(stderr, "[C] Submit Transpose2D: Invalid dimensions.\n"); return 0; }
            cl_kernel kernel = transpose_kernel_fast ? transpose_kernel_fast : transpose_kernel;
            cl_mem in = (cl_mem)cmd->buffer_input, out = (cl_mem)cmd->buffer_output;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in), "Transpose Fwd (2D) Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &out), "Transpose Fwd (2D) Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->rows), "Transpose Fwd (2D) Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->cols), "Transpose Fwd (2D) Arg 3");
            const size_t tile = 16;
            size_t gws[2] = {
                ((size_t)cmd->cols + tile - 1) / tile * tile,
                ((size_t)cmd->rows + tile - 1) / tile * tile
            };
            size_t lws[2] = { tile, tile };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 2, gws, lws, "transpose_forward"), "Transpose Fwd (2D) Enqueue");
            return 1;
        }
        case COMMAND_GELU_BACKWARD_ELEMENTWISE: {
            GeluBackwardCommandData* cmd = (GeluBackwardCommandData*)data;
            if (!gelu_backward_kernel || !cmd || !cmd->buffer_input || !cmd->buffer_grad_output || !cmd->buffer_grad_input) { fprintf(stderr, "[C] Submit GELU Bwd: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit GELU Bwd: Invalid dimensions.\n"); return 0; }
            cl_mem input_mem = (cl_mem)cmd->buffer_input; cl_mem grad_output_mem = (cl_mem)cmd->buffer_grad_output; cl_mem grad_input_mem = (cl_mem)cmd->buffer_grad_input;
            CHECK_CL_ERR(clSetKernelArg(gelu_backward_kernel, 0, sizeof(cl_mem), &input_mem), "GELU Bwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(gelu_backward_kernel, 1, sizeof(cl_mem), &grad_output_mem), "GELU Bwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(gelu_backward_kernel, 2, sizeof(cl_mem), &grad_input_mem), "GELU Bwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(gelu_backward_kernel, 3, sizeof(cl_int), &cmd->num_elements), "GELU Bwd Arg 3");
            size_t gws[1] = { (size_t)cmd->num_elements };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(gelu_backward_kernel, 1, gws, NULL, "gelu_backward"), "GELU Bwd Enqueue");
            return 1;
        }
        case COMMAND_MATMUL_BACKWARD_DA: {
            MatMulBackwardData* cmd = (MatMulBackwardData*)data;
            if ((!matmul_backward_da_kernel && !matmul_backward_da_kernel_fast) || !cmd || !cmd->buffer_dc || !cmd->buffer_b || !cmd->buffer_da) { fprintf(stderr, "[C] Submit MatMul dA: Invalid args or kernel.\n"); return 0; }
             if (cmd->B <= 0 || cmd->M <= 0 || cmd->K <= 0) { if ((size_t)cmd->B * cmd->M * cmd->K == 0) return 1; fprintf(stderr, "[C] Submit MatMul dA: Invalid dimensions B/M/K.\n"); return 0;}
             if (cmd->N <= 0) { fprintf(stderr, "[C] Submit MatMul dA: Invalid dimension N.\n"); return 0;}
            cl_kernel kernel = matmul_backward_da_kernel_fast ? matmul_backward_da_kernel_fast : matmul_backward_da_kernel;
            cl_mem dc = (cl_mem)cmd->buffer_dc, b_mem = (cl_mem)cmd->buffer_b, da = (cl_mem)cmd->buffer_da;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &dc), "MatMul dA Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &b_mem), "MatMul dA Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &da), "MatMul dA Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->B), "MatMul dA Arg 3");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->M), "MatMul dA Arg 4");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->N), "MatMul dA Arg 5");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_int), &cmd->K), "MatMul dA Arg 6");
            size_t gws[3] = { (size_t)cmd->K, (size_t)cmd->M, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 3, gws, NULL, "matmul_backward_da"), "MatMul dA Enqueue");
            return 1;
        }
        case COMMAND_MATMUL_BACKWARD_DB: {
            MatMulBackwardData* cmd = (MatMulBackwardData*)data;
            if ((!matmul_backward_db_kernel && !matmul_backward_db_kernel_fast) || !cmd || !cmd->buffer_a || !cmd->buffer_dc || !cmd->buffer_db) { fprintf(stderr, "[C] Submit MatMul dB: Invalid args or kernel.\n"); return 0; }
            if (cmd->K <= 0 || cmd->N <= 0) { if ((size_t)cmd->K * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit MatMul dB: Invalid dimensions K/N.\n"); return 0;}
            if (cmd->B <= 0 || cmd->M <= 0) { fprintf(stderr, "[C] Submit MatMul dB: Invalid dimensions B/M.\n"); return 0;}
            cl_kernel kernel = matmul_backward_db_kernel_fast ? matmul_backward_db_kernel_fast : matmul_backward_db_kernel;
            cl_mem a_mem = (cl_mem)cmd->buffer_a, dc = (cl_mem)cmd->buffer_dc, db = (cl_mem)cmd->buffer_db;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &a_mem), "MatMul dB Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &dc), "MatMul dB Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &db), "MatMul dB Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->B), "MatMul dB Arg 3");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->M), "MatMul dB Arg 4");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->N), "MatMul dB Arg 5");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_int), &cmd->K), "MatMul dB Arg 6");
            size_t gws[2] = { (size_t)cmd->N, (size_t)cmd->K };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 2, gws, NULL, "matmul_backward_db"), "MatMul dB Enqueue");
            return 1;
        }
        case COMMAND_LAYER_NORM_BACKWARD: {
            LayerNormBackwardCommandData* cmd = (LayerNormBackwardCommandData*)data;
            if (!layernorm_backward_kernel || !cmd || !cmd->buffer_dy || !cmd->buffer_x || !cmd->buffer_dx) { fprintf(stderr, "[C] Submit LayerNorm Bwd: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_rows <= 0 || cmd->row_size <= 0) { if (cmd->num_rows == 0) return 1; fprintf(stderr, "[C] Submit LayerNorm Bwd: Invalid dimensions.\n"); return 0; }
            cl_mem dy_mem = (cl_mem)cmd->buffer_dy; cl_mem x_mem = (cl_mem)cmd->buffer_x; cl_mem dx_mem = (cl_mem)cmd->buffer_dx;
            float effective_eps = (cmd->eps > 0) ? cmd->eps : 1e-5f;
            CHECK_CL_ERR(clSetKernelArg(layernorm_backward_kernel, 0, sizeof(cl_mem), &dy_mem), "LayerNorm Bwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(layernorm_backward_kernel, 1, sizeof(cl_mem), &x_mem), "LayerNorm Bwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(layernorm_backward_kernel, 2, sizeof(cl_mem), &dx_mem), "LayerNorm Bwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(layernorm_backward_kernel, 3, sizeof(cl_int), &cmd->num_rows), "LayerNorm Bwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(layernorm_backward_kernel, 4, sizeof(cl_int), &cmd->row_size), "LayerNorm Bwd Arg 4");
            CHECK_CL_ERR(clSetKernelArg(layernorm_backward_kernel, 5, sizeof(cl_float), &effective_eps), "LayerNorm Bwd Arg 5");
            size_t gws[1] = { (size_t)cmd->num_rows };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(layernorm_backward_kernel, 1, gws, NULL, "layernorm_backward"), "LayerNorm Bwd Enqueue");
            return 1;
        }
        case COMMAND_ADAM_UPDATE: {
            AdamCommandData* cmd = (AdamCommandData*)data;
            if (!adam_kernel || !cmd || !cmd->param_buffer || !cmd->grad_buffer || !cmd->m_buffer || !cmd->v_buffer) { fprintf(stderr, "[C] Submit Adam: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit Adam: Invalid dimensions.\n"); return 0; }
             if (cmd->t_step <= 0 || cmd->lr < 0.0f || cmd->beta1 < 0.0f || cmd->beta1 >= 1.0f || cmd->beta2 < 0.0f || cmd->beta2 >= 1.0f || cmd->eps < 0.0f || cmd->weight_decay < 0.0f) {
                 fprintf(stderr, "[C] Submit Adam: Invalid hyperparameters (t=%d, lr=%f, b1=%f, b2=%f, eps=%f, wd=%f).\n", cmd->t_step, cmd->lr, cmd->beta1, cmd->beta2, cmd->eps, cmd->weight_decay);
                 return 0;
             }
            cl_mem p = (cl_mem)cmd->param_buffer; cl_mem g = (cl_mem)cmd->grad_buffer; cl_mem m = (cl_mem)cmd->m_buffer; cl_mem v = (cl_mem)cmd->v_buffer;
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 0, sizeof(cl_mem), &p), "Adam Arg 0");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 1, sizeof(cl_mem), &g), "Adam Arg 1");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 2, sizeof(cl_mem), &m), "Adam Arg 2");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 3, sizeof(cl_mem), &v), "Adam Arg 3");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 4, sizeof(cl_int), &cmd->num_elements), "Adam Arg 4");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 5, sizeof(cl_float), &cmd->lr), "Adam Arg 5");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 6, sizeof(cl_float), &cmd->beta1), "Adam Arg 6");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 7, sizeof(cl_float), &cmd->beta2), "Adam Arg 7");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 8, sizeof(cl_float), &cmd->eps), "Adam Arg 8");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 9, sizeof(cl_float), &cmd->weight_decay), "Adam Arg 9");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 10, sizeof(cl_float), &cmd->beta1_t), "Adam Arg 10");
            CHECK_CL_ERR(clSetKernelArg(adam_kernel, 11, sizeof(cl_float), &cmd->beta2_t), "Adam Arg 11");
            size_t gws[1] = { (size_t)cmd->num_elements };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(adam_kernel, 1, gws, NULL, "adam_update"), "Adam Update Enqueue");
            return 1;
        }
        case COMMAND_SOFTMAX_BACKWARD: {
            SoftmaxBackwardCommandData* cmd = (SoftmaxBackwardCommandData*)data;
            if (!softmax_backward_kernel || !cmd || !cmd->buffer_dy || !cmd->buffer_y || !cmd->buffer_dx) { fprintf(stderr, "[C] Submit Softmax Bwd: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_rows <= 0 || cmd->row_size <= 0) { if (cmd->num_rows == 0) return 1; fprintf(stderr, "[C] Submit Softmax Bwd: Invalid dimensions.\n"); return 0; }
            cl_mem dy = (cl_mem)cmd->buffer_dy; cl_mem y = (cl_mem)cmd->buffer_y; cl_mem dx = (cl_mem)cmd->buffer_dx;
            CHECK_CL_ERR(clSetKernelArg(softmax_backward_kernel, 0, sizeof(cl_mem), &dy), "Softmax Bwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(softmax_backward_kernel, 1, sizeof(cl_mem), &y), "Softmax Bwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(softmax_backward_kernel, 2, sizeof(cl_mem), &dx), "Softmax Bwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(softmax_backward_kernel, 3, sizeof(cl_int), &cmd->num_rows), "Softmax Bwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(softmax_backward_kernel, 4, sizeof(cl_int), &cmd->row_size), "Softmax Bwd Arg 4");
            size_t gws[1] = { (size_t)cmd->num_rows };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(softmax_backward_kernel, 1, gws, NULL, "softmax_backward"), "Softmax Bwd Enqueue");
            return 1;
        }
         case COMMAND_MUL_BACKWARD: {
            MulBackwardCommandData* cmd = (MulBackwardCommandData*)data;
            if (!mul_backward_kernel || !cmd || !cmd->buffer_dC || !cmd->buffer_A || !cmd->buffer_B || (!cmd->buffer_dA && !cmd->buffer_dB)) {
                if (cmd && !cmd->buffer_dA && !cmd->buffer_dB) return 1;
                fprintf(stderr, "[C] Submit Mul Bwd: Invalid args or kernel.\n"); return 0;
            }
            if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit Mul Bwd: Invalid dimensions.\n"); return 0; }
            cl_mem dC = (cl_mem)cmd->buffer_dC; cl_mem A_mem = (cl_mem)cmd->buffer_A; cl_mem B_mem = (cl_mem)cmd->buffer_B;
            cl_mem dA_mem = (cl_mem)cmd->buffer_dA; cl_mem dB_mem = (cl_mem)cmd->buffer_dB;
            CHECK_CL_ERR(clSetKernelArg(mul_backward_kernel, 0, sizeof(cl_mem), &dC), "Mul Bwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(mul_backward_kernel, 1, sizeof(cl_mem), &A_mem), "Mul Bwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(mul_backward_kernel, 2, sizeof(cl_mem), &B_mem), "Mul Bwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(mul_backward_kernel, 3, sizeof(cl_mem), &dA_mem), "Mul Bwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(mul_backward_kernel, 4, sizeof(cl_mem), &dB_mem), "Mul Bwd Arg 4");
            CHECK_CL_ERR(clSetKernelArg(mul_backward_kernel, 5, sizeof(cl_int), &cmd->num_elements), "Mul Bwd Arg 5");
            size_t gws[1] = { (size_t)cmd->num_elements };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(mul_backward_kernel, 1, gws, NULL, "mul_backward"), "Mul Bwd Enqueue");
            return 1;
        }
        case COMMAND_TRANSPOSE_BACKWARD: {
            TransposeBackwardCommandData* cmd = (TransposeBackwardCommandData*)data;
            if ((!transpose_backward_kernel && !transpose_backward_kernel_fast) || !cmd || !cmd->buffer_dC || !cmd->buffer_dA ) { fprintf(stderr, "[C] Submit Transpose2D Bwd: Invalid args or kernel.\n"); return 0; }
            if (cmd->rows_A <= 0 || cmd->cols_A <= 0) { if ((size_t)cmd->rows_A * cmd->cols_A == 0) return 1; fprintf(stderr, "[C] Submit Transpose2D Bwd: Invalid dimensions.\n"); return 0; }
            cl_kernel kernel = transpose_backward_kernel_fast ? transpose_backward_kernel_fast : transpose_backward_kernel;
            cl_mem dC = (cl_mem)cmd->buffer_dC; cl_mem dA = (cl_mem)cmd->buffer_dA;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &dC), "Transpose Bwd (2D) Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &dA), "Transpose Bwd (2D) Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->rows_A), "Transpose Bwd (2D) Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->cols_A), "Transpose Bwd (2D) Arg 3");
            const size_t tile = 16;
            size_t gws[2] = {
                ((size_t)cmd->rows_A + tile - 1) / tile * tile,
                ((size_t)cmd->cols_A + tile - 1) / tile * tile
            };
            size_t lws[2] = { tile, tile };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 2, gws, lws, "transpose_backward"), "Transpose Bwd (2D) Enqueue");
            return 1;
        }
        case COMMAND_EMBEDDING_LOOKUP: {
            EmbeddingLookupCommandData* cmd = (EmbeddingLookupCommandData*)data;
            if (!embedding_lookup_kernel || !cmd || !cmd->idx || !cmd->w || !cmd->o) { fprintf(stderr, "[C] Submit Embed Lookup: Invalid args or kernel.\n"); return 0; }
            if (cmd->b <= 0 || cmd->s <= 0) { if ((size_t)cmd->b * cmd->s == 0) return 1; fprintf(stderr, "[C] Submit Embed Lookup: Invalid dimensions B/S.\n"); return 0; }
            if (cmd->d <= 0 || cmd->v <= 0) { fprintf(stderr, "[C] Submit Embed Lookup: Invalid dimensions D/V.\n"); return 0; }
            cl_mem idx_mem = (cl_mem)cmd->idx, w_mem = (cl_mem)cmd->w, o_mem = (cl_mem)cmd->o;
            CHECK_CL_ERR(clSetKernelArg(embedding_lookup_kernel, 0, sizeof(cl_mem), &idx_mem), "Embedding Lookup Arg 0");
            CHECK_CL_ERR(clSetKernelArg(embedding_lookup_kernel, 1, sizeof(cl_mem), &w_mem), "Embedding Lookup Arg 1");
            CHECK_CL_ERR(clSetKernelArg(embedding_lookup_kernel, 2, sizeof(cl_mem), &o_mem), "Embedding Lookup Arg 2");
            CHECK_CL_ERR(clSetKernelArg(embedding_lookup_kernel, 3, sizeof(cl_int), &cmd->s), "Embedding Lookup Arg 3");
            CHECK_CL_ERR(clSetKernelArg(embedding_lookup_kernel, 4, sizeof(cl_int), &cmd->d), "Embedding Lookup Arg 4");
            CHECK_CL_ERR(clSetKernelArg(embedding_lookup_kernel, 5, sizeof(cl_int), &cmd->v), "Embedding Lookup Arg 5");
            size_t gws[2] = { (size_t)cmd->s, (size_t)cmd->b };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(embedding_lookup_kernel, 2, gws, NULL, "embedding_lookup"), "Embedding Lookup Enqueue");
            return 1;
        }
        case COMMAND_EMBEDDING_BACKWARD_PASS1: {
            EmbeddingBackwardPass1CommandData* cmd = (EmbeddingBackwardPass1CommandData*)data;
            if (!embedding_backward_calc_delta_local_kernel || !cmd || !cmd->d_o || !cmd->idx || !cmd->delta_dw) { fprintf(stderr, "[C] Submit Embed Bwd P1: Invalid args or kernel.\n"); return 0; }
             if (cmd->b <= 0 || cmd->s <= 0) { if ((size_t)cmd->b * cmd->s == 0) return 1; fprintf(stderr, "[C] Submit Embed Bwd P1: Invalid dimensions B/S.\n"); return 0; }
             if (cmd->d <= 0 || cmd->v <= 0) { if ((size_t)cmd->v * cmd->d == 0) return 1; fprintf(stderr, "[C] Submit Embed Bwd P1: Invalid dimensions D/V.\n"); return 0; }
            cl_mem d_o_mem = (cl_mem)cmd->d_o; cl_mem idx_mem = (cl_mem)cmd->idx; cl_mem delta_dw_mem = (cl_mem)cmd->delta_dw;
            if (get_reduction_params_helper(&lws_reduce, &local_mem_bytes) != CL_SUCCESS) { fprintf(stderr, "[C] Submit Embed Bwd P1: Failed to get reduction parameters.\n"); return 0; }
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 0, sizeof(cl_mem), &d_o_mem), "Embed Bwd P1 Arg 0");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 1, sizeof(cl_mem), &idx_mem), "Embed Bwd P1 Arg 1");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 2, sizeof(cl_mem), &delta_dw_mem), "Embed Bwd P1 Arg 2");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 3, sizeof(cl_int), &cmd->b), "Embed Bwd P1 Arg 3 (B)");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 4, sizeof(cl_int), &cmd->s), "Embed Bwd P1 Arg 4 (S)");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 5, sizeof(cl_int), &cmd->d), "Embed Bwd P1 Arg 5 (D)");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 6, sizeof(cl_int), &cmd->v), "Embed Bwd P1 Arg 6 (V)");
            CHECK_CL_ERR(clSetKernelArg(embedding_backward_calc_delta_local_kernel, 7, local_mem_bytes, NULL), "Embed Bwd P1 Arg 7 (Local Mem)");
            size_t num_groups = (size_t)cmd->v * cmd->d;
            if (num_groups == 0) return 1;
            size_t gws_aligned[1] = { num_groups * lws_reduce };
            size_t lws[1] = { lws_reduce };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(embedding_backward_calc_delta_local_kernel, 1, gws_aligned, lws, "embedding_backward_delta"), "Embed Bwd P1 Enqueue");
            return 1;
        }
        case COMMAND_REDUCE_SUM_AXIS01: {
            ReduceSumCommandData* cmd = (ReduceSumCommandData*)data;
            if (!reduce_sum_kernel || !cmd || !cmd->in || !cmd->out) { fprintf(stderr, "[C] Submit ReduceSum01: Invalid args or kernel.\n"); return 0; }
            if (cmd->B <= 0 || cmd->M <= 0 || cmd->N <= 0) { if ((size_t)cmd->B * cmd->M == 0 || cmd->N == 0) return 1; fprintf(stderr, "[C] Submit ReduceSum01: Invalid dimensions.\n"); return 0; }
            cl_mem in_mem = (cl_mem)cmd->in, out_mem = (cl_mem)cmd->out;
            if (get_reduction_params_helper(&lws_reduce, &local_mem_bytes) != CL_SUCCESS) { fprintf(stderr, "[C] Submit ReduceSum01: Failed to get reduction parameters.\n"); return 0; }
            CHECK_CL_ERR(clSetKernelArg(reduce_sum_kernel, 0, sizeof(cl_mem), &in_mem), "ReduceSum Arg 0");
            CHECK_CL_ERR(clSetKernelArg(reduce_sum_kernel, 1, sizeof(cl_mem), &out_mem), "ReduceSum Arg 1");
            CHECK_CL_ERR(clSetKernelArg(reduce_sum_kernel, 2, sizeof(cl_int), &cmd->B), "ReduceSum Arg 2");
            CHECK_CL_ERR(clSetKernelArg(reduce_sum_kernel, 3, sizeof(cl_int), &cmd->M), "ReduceSum Arg 3");
            CHECK_CL_ERR(clSetKernelArg(reduce_sum_kernel, 4, sizeof(cl_int), &cmd->N), "ReduceSum Arg 4");
            CHECK_CL_ERR(clSetKernelArg(reduce_sum_kernel, 5, local_mem_bytes, NULL), "ReduceSum Arg 5 (Local Mem)");
            size_t num_groups = (size_t)cmd->N;
            size_t gws[1] = { num_groups * lws_reduce };
            size_t lws[1] = { lws_reduce };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(reduce_sum_kernel, 1, gws, lws, "reduce_sum_axis01"), "ReduceSum Axis01 Enqueue");
            return 1;
        }
        case COMMAND_BROADCAST_ADD_BIAS: {
            BroadcastAddCommandData* cmd = (BroadcastAddCommandData*)data;
            if (!broadcast_add_kernel || !cmd || !cmd->a || !cmd->b || !cmd->c) { fprintf(stderr, "[C] Submit BroadcastAdd: Invalid args or kernel.\n"); return 0; }
            if (cmd->B <= 0 || cmd->M <= 0 || cmd->N <= 0) { if ((size_t)cmd->B * cmd->M * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit BroadcastAdd: Invalid dimensions.\n"); return 0; }
            cl_mem a = (cl_mem)cmd->a, b_bias = (cl_mem)cmd->b, c = (cl_mem)cmd->c;
            CHECK_CL_ERR(clSetKernelArg(broadcast_add_kernel, 0, sizeof(cl_mem), &a), "BroadcastAdd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(broadcast_add_kernel, 1, sizeof(cl_mem), &b_bias), "BroadcastAdd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(broadcast_add_kernel, 2, sizeof(cl_mem), &c), "BroadcastAdd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(broadcast_add_kernel, 3, sizeof(cl_int), &cmd->M), "BroadcastAdd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(broadcast_add_kernel, 4, sizeof(cl_int), &cmd->N), "BroadcastAdd Arg 4");
            size_t gws[3] = { (size_t)cmd->N, (size_t)cmd->M, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(broadcast_add_kernel, 3, gws, NULL, "broadcast_add"), "BroadcastAdd Enqueue");
            return 1;
        }
        case COMMAND_TRANSPOSE_BATCHED: {
            TransposeBatchedCommandData* cmd = (TransposeBatchedCommandData*)data;
            if (!transpose_batched_kernel || !cmd || !cmd->in || !cmd->out) { fprintf(stderr, "[C] Submit TransposeBatched: Invalid args or kernel.\n"); return 0; }
            if (cmd->B_flat <= 0 || cmd->d1 <= 0 || cmd->d2 <= 0) { if ((size_t)cmd->B_flat * cmd->d1 * cmd->d2 == 0) return 1; fprintf(stderr, "[C] Submit TransposeBatched: Invalid dimensions.\n"); return 0; }
            cl_mem in_mem = (cl_mem)cmd->in, out_mem = (cl_mem)cmd->out;
            CHECK_CL_ERR(clSetKernelArg(transpose_batched_kernel, 0, sizeof(cl_mem), &in_mem), "TransposeBatched Arg 0");
            CHECK_CL_ERR(clSetKernelArg(transpose_batched_kernel, 1, sizeof(cl_mem), &out_mem), "TransposeBatched Arg 1");
            CHECK_CL_ERR(clSetKernelArg(transpose_batched_kernel, 2, sizeof(cl_int), &cmd->d1), "TransposeBatched Arg 2");
            CHECK_CL_ERR(clSetKernelArg(transpose_batched_kernel, 3, sizeof(cl_int), &cmd->d2), "TransposeBatched Arg 3");
            size_t gws[3] = { (size_t)cmd->d2, (size_t)cmd->d1, (size_t)cmd->B_flat };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(transpose_batched_kernel, 3, gws, NULL, "transpose_batched"), "TransposeBatched (LastTwo) Enqueue");
            return 1;
        }
        case COMMAND_MATRIX_MULTIPLY_BATCHED: {
            BMMBatchedCommandData* cmd = (BMMBatchedCommandData*)data;
             if (!matmul_batched_kernel || !cmd || !cmd->buffer_a || !cmd->buffer_b || !cmd->buffer_c) { fprintf(stderr, "[C] Submit BMM Batched: Invalid args or kernel.\n"); return 0;}
             if (cmd->B <= 0 || cmd->M <= 0 || cmd->N <= 0) { if ((size_t)cmd->B * cmd->M * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit BMM Batched: Invalid dimensions B/M/N.\n"); return 0;}
             if (cmd->K <= 0) { fprintf(stderr, "[C] Submit BMM Batched: Invalid dimension K.\n"); return 0;}
            cl_mem a = (cl_mem)cmd->buffer_a, b = (cl_mem)cmd->buffer_b, c = (cl_mem)cmd->buffer_c;
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 0, sizeof(cl_mem), &a), "BMM Batched Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 1, sizeof(cl_mem), &b), "BMM Batched Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 2, sizeof(cl_mem), &c), "BMM Batched Fwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 3, sizeof(cl_int), &cmd->B), "BMM Batched Fwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 4, sizeof(cl_int), &cmd->M), "BMM Batched Fwd Arg 4");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 5, sizeof(cl_int), &cmd->N), "BMM Batched Fwd Arg 5");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_kernel, 6, sizeof(cl_int), &cmd->K), "BMM Batched Fwd Arg 6");
            size_t gws[3] = { (size_t)cmd->N, (size_t)cmd->M, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(matmul_batched_kernel, 3, gws, NULL, "matmul_batched"), "BMM Batched Fwd Enqueue");
            return 1;
        }
        case COMMAND_MATRIX_MULTIPLY_BATCHED_BACKWARD_DA: {
            BMMBatchedBackwardData* cmd = (BMMBatchedBackwardData*)data;
            if (!matmul_batched_backward_da_kernel || !cmd || !cmd->buffer_dc || !cmd->buffer_b || !cmd->buffer_da) { fprintf(stderr, "[C] Submit BMM Batched dA: Invalid args or kernel.\n"); return 0; }
            if (cmd->B <= 0 || cmd->M <= 0 || cmd->K <= 0) { if ((size_t)cmd->B * cmd->M * cmd->K == 0) return 1; fprintf(stderr, "[C] Submit BMM Batched dA: Invalid dimensions B/M/K.\n"); return 0; }
            if (cmd->N <= 0) { fprintf(stderr, "[C] Submit BMM Batched dA: Invalid dimension N.\n"); return 0; }
            cl_mem dc = (cl_mem)cmd->buffer_dc, b_in = (cl_mem)cmd->buffer_b, da = (cl_mem)cmd->buffer_da;
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 0, sizeof(cl_mem), &dc), "MatMul Batched dA Arg 0");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 1, sizeof(cl_mem), &b_in), "MatMul Batched dA Arg 1");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 2, sizeof(cl_mem), &da), "MatMul Batched dA Arg 2");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 3, sizeof(cl_int), &cmd->B), "MatMul Batched dA Arg 3");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 4, sizeof(cl_int), &cmd->M), "MatMul Batched dA Arg 4");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 5, sizeof(cl_int), &cmd->N), "MatMul Batched dA Arg 5");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_da_kernel, 6, sizeof(cl_int), &cmd->K), "MatMul Batched dA Arg 6");
            size_t gws[3] = { (size_t)cmd->K, (size_t)cmd->M, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(matmul_batched_backward_da_kernel, 3, gws, NULL, "matmul_batched_backward_da"), "MatMul Batched dA Enqueue");
            return 1;
        }
        case COMMAND_MATRIX_MULTIPLY_BATCHED_BACKWARD_DB: {
            BMMBatchedBackwardData* cmd = (BMMBatchedBackwardData*)data;
            if (!matmul_batched_backward_db_kernel || !cmd || !cmd->buffer_a || !cmd->buffer_dc || !cmd->buffer_db) { fprintf(stderr, "[C] Submit BMM Batched dB: Invalid args or kernel.\n"); return 0; }
             if (cmd->B <= 0 || cmd->K <= 0 || cmd->N <= 0) { if ((size_t)cmd->B * cmd->K * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit BMM Batched dB: Invalid dimensions B/K/N.\n"); return 0; }
             if (cmd->M <= 0) { fprintf(stderr, "[C] Submit BMM Batched dB: Invalid dimension M.\n"); return 0; }
            cl_mem a_in = (cl_mem)cmd->buffer_a, dc = (cl_mem)cmd->buffer_dc, db = (cl_mem)cmd->buffer_db;
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 0, sizeof(cl_mem), &a_in), "MatMul Batched dB Arg 0");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 1, sizeof(cl_mem), &dc), "MatMul Batched dB Arg 1");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 2, sizeof(cl_mem), &db), "MatMul Batched dB Arg 2");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 3, sizeof(cl_int), &cmd->B), "MatMul Batched dB Arg 3");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 4, sizeof(cl_int), &cmd->M), "MatMul Batched dB Arg 4");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 5, sizeof(cl_int), &cmd->N), "MatMul Batched dB Arg 5");
            CHECK_CL_ERR(clSetKernelArg(matmul_batched_backward_db_kernel, 6, sizeof(cl_int), &cmd->K), "MatMul Batched dB Arg 6");
            size_t gws[3] = { (size_t)cmd->N, (size_t)cmd->K, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(matmul_batched_backward_db_kernel, 3, gws, NULL, "matmul_batched_backward_db"), "MatMul Batched dB Enqueue");
            return 1;
        }
        case COMMAND_TRANSPOSE_12_BATCHED: {
            Transpose12BatchedCommandData* cmd = (Transpose12BatchedCommandData*)data;
            if (!transpose_12_batched_kernel || !cmd || !cmd->in || !cmd->out) { fprintf(stderr, "[C] Submit Transpose12B: Invalid args or kernel.\n"); return 0; }
            if (cmd->B <= 0 || cmd->D1 <= 0 || cmd->D2 <= 0 || cmd->D3 <= 0) { if ((size_t)cmd->B * cmd->D1 * cmd->D2 * cmd->D3 == 0) return 1; fprintf(stderr, "[C] Submit Transpose12B: Invalid dimensions.\n"); return 0; }
            cl_mem in_mem = (cl_mem)cmd->in; cl_mem out_mem = (cl_mem)cmd->out;
            CHECK_CL_ERR(clSetKernelArg(transpose_12_batched_kernel, 0, sizeof(cl_mem), &in_mem), "Transpose12 Arg 0");
            CHECK_CL_ERR(clSetKernelArg(transpose_12_batched_kernel, 1, sizeof(cl_mem), &out_mem), "Transpose12 Arg 1");
            CHECK_CL_ERR(clSetKernelArg(transpose_12_batched_kernel, 2, sizeof(cl_int), &cmd->B), "Transpose12 Arg 2");
            CHECK_CL_ERR(clSetKernelArg(transpose_12_batched_kernel, 3, sizeof(cl_int), &cmd->D1), "Transpose12 Arg 3");
            CHECK_CL_ERR(clSetKernelArg(transpose_12_batched_kernel, 4, sizeof(cl_int), &cmd->D2), "Transpose12 Arg 4");
            CHECK_CL_ERR(clSetKernelArg(transpose_12_batched_kernel, 5, sizeof(cl_int), &cmd->D3), "Transpose12 Arg 5");
            size_t gws[3] = { (size_t)cmd->D3, (size_t)cmd->D1, (size_t)cmd->D2 * cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(transpose_12_batched_kernel, 3, gws, NULL, "transpose_12_batched"), "Transpose12Batched Enqueue");
            return 1;
        }
        case COMMAND_LOG_SOFTMAX_STABLE: {
            LogSoftmaxStableCommandData* cmd = (LogSoftmaxStableCommandData*)data;
            if ((!log_softmax_kernel && !log_softmax_kernel_fast) || !cmd || !cmd->input_logits || !cmd->output_log_probs) { fprintf(stderr, "[C] Submit LogSoftmax: Invalid args or kernel.\n"); return 0; }
            if (cmd->B_S_rows <= 0 || cmd->V_cols <= 0) { if (cmd->B_S_rows == 0) return 1; fprintf(stderr, "[C] Submit LogSoftmax: Invalid dimensions.\n"); return 0; }
            cl_mem in_logits = (cl_mem)cmd->input_logits; cl_mem out_log_probs = (cl_mem)cmd->output_log_probs;
            cl_kernel kernel = log_softmax_kernel ? log_softmax_kernel : log_softmax_kernel_fast;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in_logits), "LogSoftmaxStable Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &out_log_probs), "LogSoftmaxStable Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->B_S_rows), "LogSoftmaxStable Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->V_cols), "LogSoftmaxStable Arg 3");
            size_t workgroup = (cmd->V_cols >= 256) ? 256 : 128;
            size_t scratch_bytes = workgroup * sizeof(float);
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, scratch_bytes, NULL), "LogSoftmaxStable Arg 4 (scratch max)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, scratch_bytes, NULL), "LogSoftmaxStable Arg 5 (scratch sum)");
            size_t gws[1] = { (size_t)cmd->B_S_rows * workgroup };
            size_t lws[1] = { workgroup };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, lws, "log_softmax_stable"), "LogSoftmaxStable Enqueue");
            return 1;
        }
		case COMMAND_CROSS_ENTROPY_LOSS_GRAD: {
            CrossEntropyLossGradCommandData* cmd = (CrossEntropyLossGradCommandData*)data;
            if (!cross_entropy_kernel || !cmd || !cmd->log_probs || !cmd->target_indices || !cmd->grad_input || !cmd->loss_per_sample) { fprintf(stderr, "[C] Submit CrossEntropy: Invalid args or kernel.\n"); return 0; }
            if (cmd->B_S_rows <= 0 || cmd->V_cols <= 0) { if (cmd->B_S_rows == 0) return 1; fprintf(stderr, "[C] Submit CrossEntropy: Invalid dimensions.\n"); return 0; }
            cl_mem log_probs_mem = (cl_mem)cmd->log_probs; cl_mem target_indices_mem = (cl_mem)cmd->target_indices; cl_mem grad_input_mem = (cl_mem)cmd->grad_input; cl_mem loss_per_sample_mem = (cl_mem)cmd->loss_per_sample;
            CHECK_CL_ERR(clSetKernelArg(cross_entropy_kernel, 0, sizeof(cl_mem), &log_probs_mem), "CrossEntropyLossGrad Arg 0");
            CHECK_CL_ERR(clSetKernelArg(cross_entropy_kernel, 1, sizeof(cl_mem), &target_indices_mem), "CrossEntropyLossGrad Arg 1");
            CHECK_CL_ERR(clSetKernelArg(cross_entropy_kernel, 2, sizeof(cl_mem), &grad_input_mem), "CrossEntropyLossGrad Arg 2");
            CHECK_CL_ERR(clSetKernelArg(cross_entropy_kernel, 3, sizeof(cl_mem), &loss_per_sample_mem), "CrossEntropyLossGrad Arg 3");
            CHECK_CL_ERR(clSetKernelArg(cross_entropy_kernel, 4, sizeof(cl_int), &cmd->B_S_rows), "CrossEntropyLossGrad Arg 4 (num_rows)");
            CHECK_CL_ERR(clSetKernelArg(cross_entropy_kernel, 5, sizeof(cl_int), &cmd->V_cols), "CrossEntropyLossGrad Arg 5 (V)");
            size_t gws[1] = { (size_t)cmd->B_S_rows };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(cross_entropy_kernel, 1, gws, NULL, "cross_entropy_grad"), "CrossEntropyLossGrad Enqueue");
            return 1;
        }
        case COMMAND_ADD_BROADCAST_PE: {
            AddBroadcastPECommandData* cmd = (AddBroadcastPECommandData*)data;
            if (!add_broadcast_pe_kernel || !cmd || !cmd->input || !cmd->pe_slice || !cmd->output) { fprintf(stderr, "[C] Submit AddBroadcastPE: Invalid args or kernel.\n"); return 0; }
            if (cmd->B <= 0 || cmd->S <= 0 || cmd->E <= 0) { if ((size_t)cmd->B * cmd->S * cmd->E == 0) return 1; fprintf(stderr, "[C] Submit AddBroadcastPE: Invalid dimensions.\n"); return 0; }
            cl_mem input_mem = (cl_mem)cmd->input; cl_mem pe_slice_mem = (cl_mem)cmd->pe_slice; cl_mem output_mem = (cl_mem)cmd->output;
            CHECK_CL_ERR(clSetKernelArg(add_broadcast_pe_kernel, 0, sizeof(cl_mem), &input_mem), "AddBroadcastPE Arg 0");
            CHECK_CL_ERR(clSetKernelArg(add_broadcast_pe_kernel, 1, sizeof(cl_mem), &pe_slice_mem), "AddBroadcastPE Arg 1");
            CHECK_CL_ERR(clSetKernelArg(add_broadcast_pe_kernel, 2, sizeof(cl_mem), &output_mem), "AddBroadcastPE Arg 2");
            CHECK_CL_ERR(clSetKernelArg(add_broadcast_pe_kernel, 3, sizeof(cl_int), &cmd->S), "AddBroadcastPE Arg 3");
            CHECK_CL_ERR(clSetKernelArg(add_broadcast_pe_kernel, 4, sizeof(cl_int), &cmd->E), "AddBroadcastPE Arg 4");
            size_t gws[3] = { (size_t)cmd->E, (size_t)cmd->S, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(add_broadcast_pe_kernel, 3, gws, NULL, "add_broadcast_pe"), "AddBroadcastPE Enqueue");
            return 1;
        }
        case COMMAND_HEBBIAN_OUTER_PRODUCT_UPDATE: {
            HebbianUpdateLocalReduceCommandData* cmd = (HebbianUpdateLocalReduceCommandData*)data;
            if (!hebbian_update_local_reduce_kernel || !cmd || !cmd->buffer_a || !cmd->buffer_c || !cmd->buffer_w) { fprintf(stderr, "[C] Submit HebbianLR: Invalid args or kernel.\n"); return 0; }
            if (cmd->K <= 0 || cmd->N <= 0) { if ((size_t)cmd->K * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit HebbianLR: Invalid dimensions K/N.\n"); return 0; }
            if (cmd->B <= 0 || cmd->M <= 0) { fprintf(stderr, "[C] Submit HebbianLR: Invalid dimensions B/M.\n"); return 0; }
            cl_mem a_mem = (cl_mem)cmd->buffer_a; cl_mem c_mem = (cl_mem)cmd->buffer_c; cl_mem w_mem = (cl_mem)cmd->buffer_w;
            if (get_reduction_params_helper(&lws_reduce, &local_mem_bytes) != CL_SUCCESS) { fprintf(stderr, "[C] Submit HebbianLR: Failed to get reduction parameters.\n"); return 0; }
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 0, sizeof(cl_mem), &a_mem), "HebbianLR Arg 0 (A)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 1, sizeof(cl_mem), &c_mem), "HebbianLR Arg 1 (C)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 2, sizeof(cl_mem), &w_mem), "HebbianLR Arg 2 (W)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 3, sizeof(cl_float), &cmd->learning_rate), "HebbianLR Arg 3 (LR)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 4, sizeof(cl_int), &cmd->B), "HebbianLR Arg 4 (B)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 5, sizeof(cl_int), &cmd->M), "HebbianLR Arg 5 (M)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 6, sizeof(cl_int), &cmd->N), "HebbianLR Arg 6 (N)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 7, sizeof(cl_int), &cmd->K), "HebbianLR Arg 7 (K)");
            CHECK_CL_ERR(clSetKernelArg(hebbian_update_local_reduce_kernel, 8, local_mem_bytes, NULL), "HebbianLR Arg 8 (Local Mem)");
            size_t num_groups = (size_t)cmd->K * cmd->N;
            if (num_groups == 0) return 1;
            size_t gws_aligned[1] = { num_groups * lws_reduce };
            size_t lws[1] = { lws_reduce };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(hebbian_update_local_reduce_kernel, 1, gws_aligned, lws, "hebbian_update"), "Hebbian Update Local Reduce Enqueue");
            return 1;
        }
        case COMMAND_THRESHOLD_SPIKE: {
            ThresholdSpikeCommandData* cmd = (ThresholdSpikeCommandData*)data;
            if (!threshold_spike_kernel || !cmd || !cmd->buffer_activations || !cmd->buffer_spikes) { fprintf(stderr, "[C] Submit ThresholdSpike: Invalid args or kernel.\n"); return 0; }
            if (cmd->num_elements <= 0) { if (cmd->num_elements == 0) return 1; fprintf(stderr, "[C] Submit ThresholdSpike: Invalid dimensions.\n"); return 0; }
            cl_mem act_mem = (cl_mem)cmd->buffer_activations; cl_mem spk_mem = (cl_mem)cmd->buffer_spikes;
            CHECK_CL_ERR(clSetKernelArg(threshold_spike_kernel, 0, sizeof(cl_mem), &act_mem), "Threshold Spike Arg 0");
            CHECK_CL_ERR(clSetKernelArg(threshold_spike_kernel, 1, sizeof(cl_mem), &spk_mem), "Threshold Spike Arg 1");
            CHECK_CL_ERR(clSetKernelArg(threshold_spike_kernel, 2, sizeof(cl_float), &cmd->threshold), "Threshold Spike Arg 2");
            CHECK_CL_ERR(clSetKernelArg(threshold_spike_kernel, 3, sizeof(cl_int), &cmd->num_elements), "Threshold Spike Arg 3");
            size_t gws[1] = { (size_t)cmd->num_elements };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(threshold_spike_kernel, 1, gws, NULL, "threshold_spike"), "Threshold Spike Enqueue");
            return 1;
        }
        case COMMAND_ADD_BIAS_MN: {
             AddBiasMNCommandData* cmd = (AddBiasMNCommandData*)data;
             if (!add_bias_mn_kernel || !cmd || !cmd->a_or_c || !cmd->b_bias) { fprintf(stderr, "[C] Submit AddBiasMN: Invalid args or kernel.\n"); return 0; }
             if (cmd->M <= 0 || cmd->N <= 0) { if ((size_t)cmd->M * cmd->N == 0) return 1; fprintf(stderr, "[C] Submit AddBiasMN: Invalid dimensions.\n"); return 0; }
             cl_mem a_or_c_mem = (cl_mem)cmd->a_or_c; cl_mem b_bias_mem = (cl_mem)cmd->b_bias;
             CHECK_CL_ERR(clSetKernelArg(add_bias_mn_kernel, 0, sizeof(cl_mem), &a_or_c_mem), "AddBiasMN Arg 0 (A)");
             CHECK_CL_ERR(clSetKernelArg(add_bias_mn_kernel, 1, sizeof(cl_mem), &b_bias_mem), "AddBiasMN Arg 1 (B)");
             CHECK_CL_ERR(clSetKernelArg(add_bias_mn_kernel, 2, sizeof(cl_mem), &a_or_c_mem), "AddBiasMN Arg 2 (C)");
             CHECK_CL_ERR(clSetKernelArg(add_bias_mn_kernel, 3, sizeof(cl_int), &cmd->M), "AddBiasMN Arg 3 (M)");
             CHECK_CL_ERR(clSetKernelArg(add_bias_mn_kernel, 4, sizeof(cl_int), &cmd->N), "AddBiasMN Arg 4 (N)");
             size_t gws[2] = { (size_t)cmd->N, (size_t)cmd->M };
             CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(add_bias_mn_kernel, 2, gws, NULL, "add_bias_mn"), "AddBiasMN Enqueue");
             return 1;
        }
        case COMMAND_DYNAMIC_TOKEN_ASSIGNMENT: {
            DynamicTokenAssignmentCommandData* cmd = (DynamicTokenAssignmentCommandData*)data;
            if (!dynamic_token_assign_kernel || !cmd || !cmd->activations_bse || !cmd->prototypes_te || !cmd->output_indices_bs) { fprintf(stderr, "[C] Submit DynTokenAssign: Invalid args or kernel.\n"); return 0; }
            if (cmd->B <= 0 || cmd->S <= 0) { if ((size_t)cmd->B * cmd->S == 0) return 1; fprintf(stderr, "[C] Submit DynTokenAssign: Invalid dimensions B/S.\n"); return 0; }
            if (cmd->E <= 0 || cmd->T <= 0) { fprintf(stderr, "[C] Submit DynTokenAssign: Invalid dimensions E/T.\n"); return 0; }
            cl_mem act_mem = (cl_mem)cmd->activations_bse; cl_mem proto_mem = (cl_mem)cmd->prototypes_te; cl_mem idx_mem = (cl_mem)cmd->output_indices_bs;
            CHECK_CL_ERR(clSetKernelArg(dynamic_token_assign_kernel, 0, sizeof(cl_mem), &act_mem), "DynToken Assign Arg 0");
            CHECK_CL_ERR(clSetKernelArg(dynamic_token_assign_kernel, 1, sizeof(cl_mem), &proto_mem), "DynToken Assign Arg 1");
            CHECK_CL_ERR(clSetKernelArg(dynamic_token_assign_kernel, 2, sizeof(cl_mem), &idx_mem), "DynToken Assign Arg 2");
            CHECK_CL_ERR(clSetKernelArg(dynamic_token_assign_kernel, 3, sizeof(cl_int), &cmd->S), "DynToken Assign Arg 3");
            CHECK_CL_ERR(clSetKernelArg(dynamic_token_assign_kernel, 4, sizeof(cl_int), &cmd->E), "DynToken Assign Arg 4");
            CHECK_CL_ERR(clSetKernelArg(dynamic_token_assign_kernel, 5, sizeof(cl_int), &cmd->T), "DynToken Assign Arg 5");
            size_t gws[2] = { (size_t)cmd->S, (size_t)cmd->B };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(dynamic_token_assign_kernel, 2, gws, NULL, "dynamic_token_assignment"), "DynToken Assign Enqueue");
            return 1;
        }
        case COMMAND_PAIRWISE_SIMILARITY: {
            PairwiseSimilarityCommandData* cmd = (PairwiseSimilarityCommandData*)data;
            if (!pairwise_similarity_kernel || !cmd || !cmd->states_nd || !cmd->output_similarity_nn) { fprintf(stderr, "[C] Submit PairwiseSim: Invalid args or kernel.\n"); return 0; }
            if (cmd->N <= 0) { if (cmd->N == 0) return 1; fprintf(stderr, "[C] Submit PairwiseSim: Invalid dimension N.\n"); return 0; }
            if (cmd->D <= 0) { fprintf(stderr, "[C] Submit PairwiseSim: Invalid dimension D.\n"); return 0; }
            cl_mem states_mem = (cl_mem)cmd->states_nd; cl_mem sim_mem = (cl_mem)cmd->output_similarity_nn;
            CHECK_CL_ERR(clSetKernelArg(pairwise_similarity_kernel, 0, sizeof(cl_mem), &states_mem), "PairwiseSim Arg 0");
            CHECK_CL_ERR(clSetKernelArg(pairwise_similarity_kernel, 1, sizeof(cl_mem), &sim_mem), "PairwiseSim Arg 1");
            CHECK_CL_ERR(clSetKernelArg(pairwise_similarity_kernel, 2, sizeof(cl_int), &cmd->N), "PairwiseSim Arg 2");
            CHECK_CL_ERR(clSetKernelArg(pairwise_similarity_kernel, 3, sizeof(cl_int), &cmd->D), "PairwiseSim Arg 3");
            size_t gws[2] = { (size_t)cmd->N, (size_t)cmd->N };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(pairwise_similarity_kernel, 2, gws, NULL, "pairwise_similarity"), "PairwiseSim Enqueue");
            return 1;
        }
        case COMMAND_FUSED_DIFFUSION: {
            FusedDiffusionCommandData* cmd = (FusedDiffusionCommandData*)data;
            if ((!fused_diffusion_kernel && !fused_diffusion_kernel_fast) || !cmd || !cmd->buffer_X || !cmd->buffer_W || !cmd->buffer_O) {
                fprintf(stderr, "[C] Submit FusedDiffusion: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->B <= 0 || cmd->N <= 0 || cmd->D <= 0) {
                if ((size_t)cmd->B * cmd->N * cmd->D == 0) { return 1; }
                fprintf(stderr, "[C] Submit FusedDiffusion: Invalid dimensions (B=%d, N=%d, D=%d).\n", cmd->B, cmd->N, cmd->D);
                return 0;
            }
            cl_kernel kernel = fused_diffusion_kernel_fast ? fused_diffusion_kernel_fast : fused_diffusion_kernel;
            cl_mem x_mem = (cl_mem)cmd->buffer_X;
            cl_mem w_mem = (cl_mem)cmd->buffer_W;
            cl_mem o_mem = (cl_mem)cmd->buffer_O;
            unsigned int seed = (unsigned int)time(NULL) + g_rng_seed++;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &x_mem), "FusedDiffusion Arg 0 (X)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &w_mem), "FusedDiffusion Arg 1 (W)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &o_mem), "FusedDiffusion Arg 2 (O)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->B), "FusedDiffusion Arg 3 (B)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->N), "FusedDiffusion Arg 4 (N)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->D), "FusedDiffusion Arg 5 (D)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_float), &cmd->gamma), "FusedDiffusion Arg 6 (gamma)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 7, sizeof(cl_float), &cmd->sigma), "FusedDiffusion Arg 7 (sigma)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 8, sizeof(cl_uint), &seed), "FusedDiffusion Arg 8 (seed)");
            size_t total = (size_t)cmd->B * (size_t)cmd->N * (size_t)cmd->D;
            if (total == 0) { return 1; }
            size_t gws[1] = { total };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "fused_diffusion"), "FusedDiffusion Enqueue");
            return 1;
        }
        case COMMAND_IZHIKEVICH_STEP: {
            IzhikevichCommandData* cmd = (IzhikevichCommandData*)data;
            if ((!izhikevich_kernel && !izhikevich_kernel_fast) || !cmd || !cmd->v || !cmd->u || !cmd->i_inj || !cmd->spikes_out ||
                !cmd->p_a || !cmd->p_b || !cmd->p_c || !cmd->p_d) {
                fprintf(stderr, "[C] Submit Izhikevich: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->num_neurons <= 0) {
                if (cmd->num_neurons == 0) { return 1; }
                fprintf(stderr, "[C] Submit Izhikevich: Invalid neuron count (%d).\n", cmd->num_neurons);
                return 0;
            }
            if (cmd->dt <= 0.0f) {
                fprintf(stderr, "[C] Submit Izhikevich: Invalid dt (%f).\n", cmd->dt);
                return 0;
            }
            cl_kernel kernel = izhikevich_kernel_fast ? izhikevich_kernel_fast : izhikevich_kernel;
            cl_mem v_mem = (cl_mem)cmd->v;
            cl_mem u_mem = (cl_mem)cmd->u;
            cl_mem inj_mem = (cl_mem)cmd->i_inj;
            cl_mem spikes_mem = (cl_mem)cmd->spikes_out;
            cl_mem a_mem = (cl_mem)cmd->p_a;
            cl_mem b_mem = (cl_mem)cmd->p_b;
            cl_mem c_mem = (cl_mem)cmd->p_c;
            cl_mem d_mem = (cl_mem)cmd->p_d;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &v_mem), "Izhikevich Arg 0 (v)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &u_mem), "Izhikevich Arg 1 (u)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &inj_mem), "Izhikevich Arg 2 (i_inj)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_mem), &spikes_mem), "Izhikevich Arg 3 (spikes)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_mem), &a_mem), "Izhikevich Arg 4 (a)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_mem), &b_mem), "Izhikevich Arg 5 (b)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_mem), &c_mem), "Izhikevich Arg 6 (c)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 7, sizeof(cl_mem), &d_mem), "Izhikevich Arg 7 (d)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 8, sizeof(cl_float), &cmd->dt), "Izhikevich Arg 8 (dt)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 9, sizeof(cl_float), &cmd->threshold), "Izhikevich Arg 9 (threshold)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 10, sizeof(cl_int), &cmd->num_neurons), "Izhikevich Arg 10 (N)");
            size_t gws[1] = { (size_t)cmd->num_neurons };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "izhikevich_neuron_step"), "Izhikevich Enqueue");
            return 1;
        }
        case COMMAND_STDP_UPDATE: {
            STDPUpdateCommandData* cmd = (STDPUpdateCommandData*)data;
            if ((!stdp_update_kernel && !stdp_update_kernel_fast) || !cmd || !cmd->weights || !cmd->pre_traces || !cmd->post_traces ||
                !cmd->pre_spike_events || !cmd->post_spike_events) {
                fprintf(stderr, "[C] Submit STDP Update: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->pre_n <= 0 || cmd->post_n <= 0) {
                if ((cmd->pre_n == 0) || (cmd->post_n == 0)) { return 1; }
                fprintf(stderr, "[C] Submit STDP Update: Invalid dimensions (pre=%d, post=%d).\n", cmd->pre_n, cmd->post_n);
                return 0;
            }
            cl_kernel kernel = stdp_update_kernel_fast ? stdp_update_kernel_fast : stdp_update_kernel;
            cl_mem w_mem = (cl_mem)cmd->weights;
            cl_mem pre_trace_mem = (cl_mem)cmd->pre_traces;
            cl_mem post_trace_mem = (cl_mem)cmd->post_traces;
            cl_mem pre_evt_mem = (cl_mem)cmd->pre_spike_events;
            cl_mem post_evt_mem = (cl_mem)cmd->post_spike_events;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &w_mem), "STDP Update Arg 0 (weights)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &pre_trace_mem), "STDP Update Arg 1 (pre_traces)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &post_trace_mem), "STDP Update Arg 2 (post_traces)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_mem), &pre_evt_mem), "STDP Update Arg 3 (pre_events)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_mem), &post_evt_mem), "STDP Update Arg 4 (post_events)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_float), &cmd->lr_ltp), "STDP Update Arg 5 (lr_ltp)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_float), &cmd->lr_ltd), "STDP Update Arg 6 (lr_ltd)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 7, sizeof(cl_int), &cmd->pre_n), "STDP Update Arg 7 (pre_n)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 8, sizeof(cl_int), &cmd->post_n), "STDP Update Arg 8 (post_n)");
            size_t total = (size_t)cmd->pre_n * (size_t)cmd->post_n;
            size_t gws[1] = { total };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "stdp_update_step"), "STDP Update Enqueue");
            return 1;
        }
        case COMMAND_STDP_TRACE_UPDATE: {
            STDPTraceCommandData* cmd = (STDPTraceCommandData*)data;
            if ((!stdp_trace_kernel && !stdp_trace_kernel_fast) || !cmd || !cmd->pre_traces || !cmd->post_traces ||
                !cmd->pre_spike_events || !cmd->post_spike_events) {
                fprintf(stderr, "[C] Submit STDP Trace: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->pre_n < 0 || cmd->post_n < 0) {
                fprintf(stderr, "[C] Submit STDP Trace: Negative dimensions (pre=%d, post=%d).\n", cmd->pre_n, cmd->post_n);
                return 0;
            }
            int max_dim = (cmd->pre_n > cmd->post_n) ? cmd->pre_n : cmd->post_n;
            if (max_dim <= 0) { return 1; }
            cl_kernel kernel = stdp_trace_kernel_fast ? stdp_trace_kernel_fast : stdp_trace_kernel;
            cl_mem pre_trace_mem = (cl_mem)cmd->pre_traces;
            cl_mem post_trace_mem = (cl_mem)cmd->post_traces;
            cl_mem pre_evt_mem = (cl_mem)cmd->pre_spike_events;
            cl_mem post_evt_mem = (cl_mem)cmd->post_spike_events;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &pre_trace_mem), "STDP Trace Arg 0 (pre_traces)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &post_trace_mem), "STDP Trace Arg 1 (post_traces)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &pre_evt_mem), "STDP Trace Arg 2 (pre_events)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_mem), &post_evt_mem), "STDP Trace Arg 3 (post_events)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_float), &cmd->decay_pre), "STDP Trace Arg 4 (decay_pre)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_float), &cmd->decay_post), "STDP Trace Arg 5 (decay_post)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_float), &cmd->increment_pre), "STDP Trace Arg 6 (inc_pre)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 7, sizeof(cl_float), &cmd->increment_post), "STDP Trace Arg 7 (inc_post)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 8, sizeof(cl_int), &cmd->pre_n), "STDP Trace Arg 8 (pre_n)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 9, sizeof(cl_int), &cmd->post_n), "STDP Trace Arg 9 (post_n)");
            size_t gws[1] = { (size_t)max_dim };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "stdp_update_traces"), "STDP Trace Enqueue");
            return 1;
        }
        case COMMAND_LBM_COLLIDE_STREAM: {
            LBMCollideStreamCommandData* cmd = (LBMCollideStreamCommandData*)data;
            if ((!lbm_kernel && !lbm_kernel_fast) || !cmd || !cmd->f_in || !cmd->f_out || !cmd->rho || !cmd->ux || !cmd->uy) {
                fprintf(stderr, "[C] Submit LBM: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->width <= 0 || cmd->height <= 0) {
                if ((cmd->width == 0) || (cmd->height == 0)) { return 1; }
                fprintf(stderr, "[C] Submit LBM: Invalid grid dimensions (w=%d, h=%d).\n", cmd->width, cmd->height);
                return 0;
            }
            if (cmd->omega <= 0.0f) {
                fprintf(stderr, "[C] Submit LBM: Invalid relaxation omega (%f).\n", cmd->omega);
                return 0;
            }
            cl_kernel kernel = lbm_kernel_fast ? lbm_kernel_fast : lbm_kernel;
            cl_mem fin_mem = (cl_mem)cmd->f_in;
            cl_mem fout_mem = (cl_mem)cmd->f_out;
            cl_mem rho_mem = (cl_mem)cmd->rho;
            cl_mem ux_mem = (cl_mem)cmd->ux;
            cl_mem uy_mem = (cl_mem)cmd->uy;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &fin_mem), "LBM Arg 0 (f_in)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &fout_mem), "LBM Arg 1 (f_out)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &rho_mem), "LBM Arg 2 (rho)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_mem), &ux_mem), "LBM Arg 3 (ux)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_mem), &uy_mem), "LBM Arg 4 (uy)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_float), &cmd->omega), "LBM Arg 5 (omega)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_int), &cmd->width), "LBM Arg 6 (width)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 7, sizeof(cl_int), &cmd->height), "LBM Arg 7 (height)");
            size_t total = (size_t)cmd->width * (size_t)cmd->height;
            size_t gws[1] = { total };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "lbm_collide_and_stream"), "LBM Enqueue");
            return 1;
        }
        case COMMAND_NBODY_FORCES: {
            NBodyForcesCommandData* cmd = (NBodyForcesCommandData*)data;
            if ((!nbody_forces_kernel && !nbody_forces_kernel_fast) || !cmd || !cmd->positions || !cmd->forces) {
                fprintf(stderr, "[C] Submit NBody Forces: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->num_bodies <= 0) {
                if (cmd->num_bodies == 0) { return 1; }
                fprintf(stderr, "[C] Submit NBody Forces: Invalid body count (%d).\n", cmd->num_bodies);
                return 0;
            }
            cl_kernel kernel = nbody_forces_kernel_fast ? nbody_forces_kernel_fast : nbody_forces_kernel;
            cl_mem pos_mem = (cl_mem)cmd->positions;
            cl_mem force_mem = (cl_mem)cmd->forces;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &pos_mem), "NBody Forces Arg 0 (positions)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &force_mem), "NBody Forces Arg 1 (forces)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_float), &cmd->gravitational_const), "NBody Forces Arg 2 (G)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_float), &cmd->softening_factor), "NBody Forces Arg 3 (softening)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->num_bodies), "NBody Forces Arg 4 (N)");
            size_t gws[1] = { (size_t)cmd->num_bodies };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "nbody_calculate_forces"), "NBody Forces Enqueue");
            return 1;
        }
        case COMMAND_NBODY_INTEGRATE: {
            NBodyIntegrateCommandData* cmd = (NBodyIntegrateCommandData*)data;
            if ((!nbody_integrate_kernel && !nbody_integrate_kernel_fast) || !cmd || !cmd->positions || !cmd->velocities || !cmd->forces) {
                fprintf(stderr, "[C] Submit NBody Integrate: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->num_bodies <= 0) {
                if (cmd->num_bodies == 0) { return 1; }
                fprintf(stderr, "[C] Submit NBody Integrate: Invalid body count (%d).\n", cmd->num_bodies);
                return 0;
            }
            cl_kernel kernel = nbody_integrate_kernel_fast ? nbody_integrate_kernel_fast : nbody_integrate_kernel;
            cl_mem pos_mem = (cl_mem)cmd->positions;
            cl_mem vel_mem = (cl_mem)cmd->velocities;
            cl_mem force_mem = (cl_mem)cmd->forces;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &pos_mem), "NBody Integrate Arg 0 (positions)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &vel_mem), "NBody Integrate Arg 1 (velocities)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &force_mem), "NBody Integrate Arg 2 (forces)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_float), &cmd->dt), "NBody Integrate Arg 3 (dt)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->num_bodies), "NBody Integrate Arg 4 (N)");
            size_t gws[1] = { (size_t)cmd->num_bodies };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "nbody_integrate"), "NBody Integrate Enqueue");
            return 1;
        }
        case COMMAND_ISING_METROPOLIS: {
            IsingMetropolisCommandData* cmd = (IsingMetropolisCommandData*)data;
            if ((!ising_kernel && !ising_kernel_fast) || !cmd || !cmd->spin_grid || !cmd->random_numbers) {
                fprintf(stderr, "[C] Submit Ising: Invalid args or kernel.\n");
                return 0;
            }
            if (cmd->width <= 0 || cmd->height <= 0) {
                if (cmd->width == 0 || cmd->height == 0) { return 1; }
                fprintf(stderr, "[C] Submit Ising: Invalid grid dimensions (w=%d, h=%d).\n", cmd->width, cmd->height);
                return 0;
            }
            if ((cmd->color & ~1) != 0) {
                fprintf(stderr, "[C] Submit Ising: Invalid checkerboard color (%d).\n", cmd->color);
                return 0;
            }
            cl_kernel kernel = ising_kernel_fast ? ising_kernel_fast : ising_kernel;
            cl_mem spin_mem = (cl_mem)cmd->spin_grid;
            cl_mem rand_mem = (cl_mem)cmd->random_numbers;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &spin_mem), "Ising Arg 0 (spins)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &rand_mem), "Ising Arg 1 (random)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_float), &cmd->J), "Ising Arg 2 (J)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_float), &cmd->beta), "Ising Arg 3 (beta)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->width), "Ising Arg 4 (width)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->height), "Ising Arg 5 (height)");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_int), &cmd->color), "Ising Arg 6 (color)");
            size_t total = (size_t)cmd->width * (size_t)cmd->height;
            size_t gws[1] = { total };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "ising_metropolis_step"), "Ising Enqueue");
            return 1;
        }
        case COMMAND_PROTO_SEGMENTED_SUM: {
            ProtoSegmentedSumCommandData* cmd = (ProtoSegmentedSumCommandData*)data;
            if (!proto_segmented_sum_kernel || !cmd || !cmd->activations_flat || !cmd->indices_flat || !cmd->proto_sums || !cmd->proto_counts) { fprintf(stderr, "[C] Submit Proto Segmented Sum: Error - Invalid arguments or kernel handle missing.\n"); return 0; }
            if (!has_atomics_support) { fprintf(stderr, "[C] Submit Proto Segmented Sum: Error - Required atomic operations not supported by the device/driver! Cannot execute.\n"); return 0; }
            if (cmd->M_flat <= 0) { if (cmd->M_flat == 0) return 1; fprintf(stderr, "[C] Submit Proto Segmented Sum: Invalid dimension M_flat.\n"); return 0;}
            if (cmd->E <= 0 || cmd->T <= 0) { fprintf(stderr, "[C] Submit Proto Segmented Sum: Invalid dimensions E/T.\n"); return 0;}
            cl_mem act_mem = (cl_mem)cmd->activations_flat; cl_mem idx_mem = (cl_mem)cmd->indices_flat; cl_mem sums_mem = (cl_mem)cmd->proto_sums; cl_mem counts_mem = (cl_mem)cmd->proto_counts;
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 0, sizeof(cl_mem), &act_mem), "ProtoSum Arg 0");
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 1, sizeof(cl_mem), &idx_mem), "ProtoSum Arg 1");
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 2, sizeof(cl_mem), &sums_mem), "ProtoSum Arg 2");
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 3, sizeof(cl_mem), &counts_mem), "ProtoSum Arg 3");
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 4, sizeof(cl_int), &cmd->M_flat), "ProtoSum Arg 4");
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 5, sizeof(cl_int), &cmd->E), "ProtoSum Arg 5");
            CHECK_CL_ERR(clSetKernelArg(proto_segmented_sum_kernel, 6, sizeof(cl_int), &cmd->T), "ProtoSum Arg 6");
            size_t gws[1] = { (size_t)cmd->M_flat };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(proto_segmented_sum_kernel, 1, gws, NULL, "proto_segmented_sum"), "Proto Segmented Sum Enqueue");
            return 1;
        }
        case COMMAND_PROTO_UPDATE_STEP: {
            ProtoUpdateStepCommandData* cmd = (ProtoUpdateStepCommandData*)data;
            if (!proto_update_step_kernel || !cmd || !cmd->prototypes || !cmd->proto_sums || !cmd->proto_counts) { fprintf(stderr, "[C] Submit Proto Update Step: Error - Invalid arguments or kernel handle missing.\n"); return 0; }
            if (cmd->T <= 0) { if (cmd->T == 0) return 1; fprintf(stderr, "[C] Submit Proto Update Step: Invalid dimension T.\n"); return 0;}
            if (cmd->E <= 0) { fprintf(stderr, "[C] Submit Proto Update Step: Invalid dimension E.\n"); return 0;}
            if (cmd->learning_rate < 0.0f || cmd->learning_rate > 1.0f) { fprintf(stderr, "[C] Submit Proto Update Step: Warning - Invalid learning_rate (%f). Should be in [0, 1].\n", cmd->learning_rate); }
            cl_mem proto_mem = (cl_mem)cmd->prototypes; cl_mem sums_mem = (cl_mem)cmd->proto_sums; cl_mem counts_mem = (cl_mem)cmd->proto_counts;
            CHECK_CL_ERR(clSetKernelArg(proto_update_step_kernel, 0, sizeof(cl_mem), &proto_mem), "ProtoUpdate Arg 0");
            CHECK_CL_ERR(clSetKernelArg(proto_update_step_kernel, 1, sizeof(cl_mem), &sums_mem), "ProtoUpdate Arg 1");
            CHECK_CL_ERR(clSetKernelArg(proto_update_step_kernel, 2, sizeof(cl_mem), &counts_mem), "ProtoUpdate Arg 2");
            CHECK_CL_ERR(clSetKernelArg(proto_update_step_kernel, 3, sizeof(cl_float), &cmd->learning_rate), "ProtoUpdate Arg 3");
            CHECK_CL_ERR(clSetKernelArg(proto_update_step_kernel, 4, sizeof(cl_int), &cmd->E), "ProtoUpdate Arg 4");
            CHECK_CL_ERR(clSetKernelArg(proto_update_step_kernel, 5, sizeof(cl_int), &cmd->T), "ProtoUpdate Arg 5");
            size_t gws[1] = { (size_t)cmd->T };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(proto_update_step_kernel, 1, gws, NULL, "proto_update_step"), "Proto Update Step Enqueue");
            return 1;
        }
        case COMMAND_SHAPE_LOSS_REWARD_PENALTY: {
            ShapeLossRewardPenaltyCommandData* cmd = (ShapeLossRewardPenaltyCommandData*)data;
            if (!shape_loss_reward_penalty_kernel || !cmd || !cmd->loss_per_sample_in || !cmd->predictions || !cmd->targets || !cmd->loss_per_sample_out) {
                fprintf(stderr, "[C] Submit ShapeLoss: Invalid args or kernel.\n"); return 0;
            }
            if (cmd->num_samples <= 0 || cmd->num_classes <= 0) {
                if (cmd->num_samples == 0) return 1;
                fprintf(stderr, "[C] Submit ShapeLoss: Invalid dimensions (samples=%d, classes=%d).\n", cmd->num_samples, cmd->num_classes); return 0;
            }
             if (cmd->penalty_weight < 0.0f || cmd->reward_weight < 0.0f || cmd->high_confidence_threshold < 0.0f || cmd->high_confidence_threshold > 1.0f || cmd->critical_target_class < 0 || cmd->critical_target_class >= cmd->num_classes || cmd->critical_predicted_class < 0 || cmd->critical_predicted_class >= cmd->num_classes) {
                 fprintf(stderr, "[C] Submit ShapeLoss: Warning - Potentially invalid shaping parameters provided (penalty=%.2f, reward=%.2f, thresh=%.2f, crit_target=%d, crit_pred=%d).\n",
                         cmd->penalty_weight, cmd->reward_weight, cmd->high_confidence_threshold, cmd->critical_target_class, cmd->critical_predicted_class);
             }
            cl_mem loss_in_mem = (cl_mem)cmd->loss_per_sample_in; cl_mem pred_mem = (cl_mem)cmd->predictions; cl_mem targets_mem = (cl_mem)cmd->targets; cl_mem loss_out_mem = (cl_mem)cmd->loss_per_sample_out;
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 0, sizeof(cl_mem), &loss_in_mem), "ShapeLoss Arg 0 (loss_in)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 1, sizeof(cl_mem), &pred_mem), "ShapeLoss Arg 1 (predictions)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 2, sizeof(cl_mem), &targets_mem), "ShapeLoss Arg 2 (targets)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 3, sizeof(cl_mem), &loss_out_mem), "ShapeLoss Arg 3 (loss_out)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 4, sizeof(cl_int), &cmd->num_samples), "ShapeLoss Arg 4 (num_samples)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 5, sizeof(cl_int), &cmd->num_classes), "ShapeLoss Arg 5 (num_classes)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 6, sizeof(cl_float), &cmd->penalty_weight), "ShapeLoss Arg 6 (penalty_weight)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 7, sizeof(cl_float), &cmd->reward_weight), "ShapeLoss Arg 7 (reward_weight)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 8, sizeof(cl_float), &cmd->high_confidence_threshold), "ShapeLoss Arg 8 (high_confidence_threshold)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 9, sizeof(cl_int), &cmd->critical_target_class), "ShapeLoss Arg 9 (critical_target_class)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_kernel, 10, sizeof(cl_int), &cmd->critical_predicted_class), "ShapeLoss Arg 10 (critical_predicted_class)");
            size_t gws[1] = { (size_t)cmd->num_samples };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(shape_loss_reward_penalty_kernel, 1, gws, NULL, "shape_loss_reward_penalty"), "Shape Loss Reward/Penalty Enqueue");
            return 1;
        }

        // --- NEU: Loss Shaping (List) ---
        case COMMAND_SHAPE_LOSS_REWARD_PENALTY_LIST: {
            ShapeLossRewardPenaltyListCommandData* cmd = (ShapeLossRewardPenaltyListCommandData*)data;
            if (!shape_loss_reward_penalty_list_kernel || !cmd || !cmd->loss_per_sample_in || !cmd->predictions || !cmd->targets || !cmd->loss_per_sample_out) {
                fprintf(stderr, "[C] Submit ShapeLossList: Invalid args or kernel.\n"); return 0;
            }
            // Prüfe kritischen Paar-Buffer nur, wenn Paare > 0 sind
            if (cmd->num_critical_pairs > 0 && !cmd->critical_pairs) {
                 fprintf(stderr, "[C] Submit ShapeLossList: Critical pairs buffer is NULL but count > 0.\n"); return 0;
            }
            if (cmd->num_samples <= 0 || cmd->num_classes <= 0) {
                if (cmd->num_samples == 0) return 1; // Trivial case
                fprintf(stderr, "[C] Submit ShapeLossList: Invalid dimensions (samples=%d, classes=%d).\n", cmd->num_samples, cmd->num_classes); return 0;
            }
             // Basic validation of parameters
             if (cmd->penalty_weight < 0.0f || cmd->reward_weight < 0.0f || cmd->high_confidence_threshold < 0.0f || cmd->high_confidence_threshold > 1.0f || cmd->num_critical_pairs < 0) {
                 fprintf(stderr, "[C] Submit ShapeLossList: Warning - Potentially invalid shaping parameters provided (penalty=%.2f, reward=%.2f, thresh=%.2f, num_pairs=%d).\n",
                         cmd->penalty_weight, cmd->reward_weight, cmd->high_confidence_threshold, cmd->num_critical_pairs);
             }

            cl_mem loss_in_mem = (cl_mem)cmd->loss_per_sample_in;
            cl_mem pred_mem = (cl_mem)cmd->predictions;
            cl_mem targets_mem = (cl_mem)cmd->targets;
            cl_mem loss_out_mem = (cl_mem)cmd->loss_per_sample_out;
            cl_mem crit_pairs_mem = (cl_mem)cmd->critical_pairs; // Handle zum Paar-Buffer

            // Argument Indices anpassen!
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 0, sizeof(cl_mem), &loss_in_mem), "ShapeLossList Arg 0 (loss_in)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 1, sizeof(cl_mem), &pred_mem), "ShapeLossList Arg 1 (predictions)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 2, sizeof(cl_mem), &targets_mem), "ShapeLossList Arg 2 (targets)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 3, sizeof(cl_mem), &loss_out_mem), "ShapeLossList Arg 3 (loss_out)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 4, sizeof(cl_mem), &crit_pairs_mem), "ShapeLossList Arg 4 (critical_pairs)"); // NEU
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 5, sizeof(cl_int), &cmd->num_samples), "ShapeLossList Arg 5 (num_samples)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 6, sizeof(cl_int), &cmd->num_classes), "ShapeLossList Arg 6 (num_classes)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 7, sizeof(cl_int), &cmd->num_critical_pairs), "ShapeLossList Arg 7 (num_critical_pairs)"); // NEU
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 8, sizeof(cl_float), &cmd->penalty_weight), "ShapeLossList Arg 8 (penalty_weight)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 9, sizeof(cl_float), &cmd->reward_weight), "ShapeLossList Arg 9 (reward_weight)");
            CHECK_CL_ERR(clSetKernelArg(shape_loss_reward_penalty_list_kernel, 10, sizeof(cl_float), &cmd->high_confidence_threshold), "ShapeLossList Arg 10 (high_confidence_threshold)");

            size_t gws[1] = { (size_t)cmd->num_samples };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(shape_loss_reward_penalty_list_kernel, 1, gws, NULL, "shape_loss_reward_penalty_list"), "Shape Loss Reward/Penalty List Enqueue");
            return 1;
        }
        // --- Ende NEU: Loss Shaping (List) ---

        case COMMAND_CONV2D_FORWARD: {
            Conv2DForwardCommandData* cmd = (Conv2DForwardCommandData*)data;
            if (!cmd || !cmd->input || !cmd->weights || !cmd->output) {
                fprintf(stderr, "[C] Submit Conv2D Forward: Invalid command data or buffers.\n");
                return 0;
            }
            if (cmd->B <= 0 || cmd->C_in <= 0 || cmd->H <= 0 || cmd->W <= 0 ||
                cmd->C_out <= 0 || cmd->K_h <= 0 || cmd->K_w <= 0 ||
                cmd->stride_h <= 0 || cmd->stride_w <= 0 ||
                cmd->out_h <= 0 || cmd->out_w <= 0) {
                if ((size_t)cmd->B * cmd->C_out * cmd->out_h * cmd->out_w == 0) { return 1; }
                fprintf(stderr, "[C] Submit Conv2D Forward: Invalid dimensions.\n");
                return 0;
            }
            cl_kernel kernel = conv2d_forward_kernel_fast ? conv2d_forward_kernel_fast : conv2d_forward_kernel;
            if (!kernel) { fprintf(stderr, "[C] Submit Conv2D Forward: Kernel not compiled.\n"); return 0; }
            cl_mem in_mem = (cl_mem)cmd->input;
            cl_mem w_mem = (cl_mem)cmd->weights;
            cl_mem b_mem = cmd->bias ? (cl_mem)cmd->bias : NULL;
            cl_mem out_mem = (cl_mem)cmd->output;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in_mem), "Conv2D Fwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &w_mem), "Conv2D Fwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_mem), &b_mem), "Conv2D Fwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_mem), &out_mem), "Conv2D Fwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->B), "Conv2D Fwd Arg 4");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->C_in), "Conv2D Fwd Arg 5");
            CHECK_CL_ERR(clSetKernelArg(kernel, 6, sizeof(cl_int), &cmd->H), "Conv2D Fwd Arg 6");
            CHECK_CL_ERR(clSetKernelArg(kernel, 7, sizeof(cl_int), &cmd->W), "Conv2D Fwd Arg 7");
            CHECK_CL_ERR(clSetKernelArg(kernel, 8, sizeof(cl_int), &cmd->C_out), "Conv2D Fwd Arg 8");
            CHECK_CL_ERR(clSetKernelArg(kernel, 9, sizeof(cl_int), &cmd->K_h), "Conv2D Fwd Arg 9");
            CHECK_CL_ERR(clSetKernelArg(kernel,10, sizeof(cl_int), &cmd->K_w), "Conv2D Fwd Arg 10");
            CHECK_CL_ERR(clSetKernelArg(kernel,11, sizeof(cl_int), &cmd->stride_h), "Conv2D Fwd Arg 11");
            CHECK_CL_ERR(clSetKernelArg(kernel,12, sizeof(cl_int), &cmd->stride_w), "Conv2D Fwd Arg 12");
            CHECK_CL_ERR(clSetKernelArg(kernel,13, sizeof(cl_int), &cmd->out_h), "Conv2D Fwd Arg 13");
            CHECK_CL_ERR(clSetKernelArg(kernel,14, sizeof(cl_int), &cmd->out_w), "Conv2D Fwd Arg 14");
            size_t gws[1] = { (size_t)cmd->B * cmd->C_out * cmd->out_h * cmd->out_w };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "conv2d_forward"), "Conv2D Forward Enqueue");
            return 1;
        }
        case COMMAND_CONV2D_BACKWARD: {
            Conv2DBackwardCommandData* cmd = (Conv2DBackwardCommandData*)data;
            if (!cmd || !cmd->grad_output || !cmd->input || !cmd->weights) {
                fprintf(stderr, "[C] Submit Conv2D Backward: Missing required buffers.\n");
                return 0;
            }
            if (!cmd->grad_input && !cmd->grad_weights && !cmd->grad_bias) { return 1; }
            if (cmd->B <= 0 || cmd->C_in <= 0 || cmd->H <= 0 || cmd->W <= 0 ||
                cmd->C_out <= 0 || cmd->K_h <= 0 || cmd->K_w <= 0 ||
                cmd->stride_h <= 0 || cmd->stride_w <= 0 ||
                cmd->out_h <= 0 || cmd->out_w <= 0) {
                if ((size_t)cmd->B * cmd->C_out * cmd->out_h * cmd->out_w == 0) { return 1; }
                fprintf(stderr, "[C] Submit Conv2D Backward: Invalid dimensions.\n");
                return 0;
            }
            cl_kernel k_input = (cmd->grad_input && conv2d_backward_input_kernel_fast)
                                    ? conv2d_backward_input_kernel_fast : conv2d_backward_input_kernel;
            cl_kernel k_weight = (cmd->grad_weights && conv2d_backward_weight_kernel_fast)
                                    ? conv2d_backward_weight_kernel_fast : conv2d_backward_weight_kernel;
            cl_kernel k_bias = (cmd->grad_bias && conv2d_bias_grad_kernel_fast)
                                    ? conv2d_bias_grad_kernel_fast : conv2d_bias_grad_kernel;
            cl_mem go_mem = (cl_mem)cmd->grad_output;
            cl_mem in_mem = (cl_mem)cmd->input;
            cl_mem w_mem = (cl_mem)cmd->weights;
            if (cmd->grad_input && k_input) {
                cl_mem gi_mem = (cl_mem)cmd->grad_input;
                CHECK_CL_ERR(clSetKernelArg(k_input, 0, sizeof(cl_mem), &go_mem), "Conv2D dInput Arg 0");
                CHECK_CL_ERR(clSetKernelArg(k_input, 1, sizeof(cl_mem), &w_mem), "Conv2D dInput Arg 1");
                CHECK_CL_ERR(clSetKernelArg(k_input, 2, sizeof(cl_mem), &gi_mem), "Conv2D dInput Arg 2");
                CHECK_CL_ERR(clSetKernelArg(k_input, 3, sizeof(cl_int), &cmd->B), "Conv2D dInput Arg 3");
                CHECK_CL_ERR(clSetKernelArg(k_input, 4, sizeof(cl_int), &cmd->C_in), "Conv2D dInput Arg 4");
                CHECK_CL_ERR(clSetKernelArg(k_input, 5, sizeof(cl_int), &cmd->H), "Conv2D dInput Arg 5");
                CHECK_CL_ERR(clSetKernelArg(k_input, 6, sizeof(cl_int), &cmd->W), "Conv2D dInput Arg 6");
                CHECK_CL_ERR(clSetKernelArg(k_input, 7, sizeof(cl_int), &cmd->C_out), "Conv2D dInput Arg 7");
                CHECK_CL_ERR(clSetKernelArg(k_input, 8, sizeof(cl_int), &cmd->K_h), "Conv2D dInput Arg 8");
                CHECK_CL_ERR(clSetKernelArg(k_input, 9, sizeof(cl_int), &cmd->K_w), "Conv2D dInput Arg 9");
                CHECK_CL_ERR(clSetKernelArg(k_input,10, sizeof(cl_int), &cmd->stride_h), "Conv2D dInput Arg 10");
                CHECK_CL_ERR(clSetKernelArg(k_input,11, sizeof(cl_int), &cmd->stride_w), "Conv2D dInput Arg 11");
                CHECK_CL_ERR(clSetKernelArg(k_input,12, sizeof(cl_int), &cmd->out_h), "Conv2D dInput Arg 12");
                CHECK_CL_ERR(clSetKernelArg(k_input,13, sizeof(cl_int), &cmd->out_w), "Conv2D dInput Arg 13");
                size_t gws_in[1] = { (size_t)cmd->B * cmd->C_in * cmd->H * cmd->W };
                CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(k_input, 1, gws_in, NULL, "conv2d_backward_input"), "Conv2D Backward Input Enqueue");
            }
            if (cmd->grad_weights && k_weight) {
                cl_mem gw_mem = (cl_mem)cmd->grad_weights;
                CHECK_CL_ERR(clSetKernelArg(k_weight, 0, sizeof(cl_mem), &go_mem), "Conv2D dWeight Arg 0");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 1, sizeof(cl_mem), &in_mem), "Conv2D dWeight Arg 1");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 2, sizeof(cl_mem), &gw_mem), "Conv2D dWeight Arg 2");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 3, sizeof(cl_int), &cmd->B), "Conv2D dWeight Arg 3");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 4, sizeof(cl_int), &cmd->C_in), "Conv2D dWeight Arg 4");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 5, sizeof(cl_int), &cmd->H), "Conv2D dWeight Arg 5");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 6, sizeof(cl_int), &cmd->W), "Conv2D dWeight Arg 6");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 7, sizeof(cl_int), &cmd->C_out), "Conv2D dWeight Arg 7");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 8, sizeof(cl_int), &cmd->K_h), "Conv2D dWeight Arg 8");
                CHECK_CL_ERR(clSetKernelArg(k_weight, 9, sizeof(cl_int), &cmd->K_w), "Conv2D dWeight Arg 9");
                CHECK_CL_ERR(clSetKernelArg(k_weight,10, sizeof(cl_int), &cmd->stride_h), "Conv2D dWeight Arg 10");
                CHECK_CL_ERR(clSetKernelArg(k_weight,11, sizeof(cl_int), &cmd->stride_w), "Conv2D dWeight Arg 11");
                CHECK_CL_ERR(clSetKernelArg(k_weight,12, sizeof(cl_int), &cmd->out_h), "Conv2D dWeight Arg 12");
                CHECK_CL_ERR(clSetKernelArg(k_weight,13, sizeof(cl_int), &cmd->out_w), "Conv2D dWeight Arg 13");
                size_t gws_w[1] = { (size_t)cmd->C_out * cmd->C_in * cmd->K_h * cmd->K_w };
                CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(k_weight, 1, gws_w, NULL, "conv2d_backward_weight"), "Conv2D Backward Weight Enqueue");
            }
            if (cmd->grad_bias && k_bias) {
                cl_mem gb_mem = (cl_mem)cmd->grad_bias;
                CHECK_CL_ERR(clSetKernelArg(k_bias, 0, sizeof(cl_mem), &go_mem), "Conv2D dBias Arg 0");
                CHECK_CL_ERR(clSetKernelArg(k_bias, 1, sizeof(cl_mem), &gb_mem), "Conv2D dBias Arg 1");
                CHECK_CL_ERR(clSetKernelArg(k_bias, 2, sizeof(cl_int), &cmd->B), "Conv2D dBias Arg 2");
                CHECK_CL_ERR(clSetKernelArg(k_bias, 3, sizeof(cl_int), &cmd->C_out), "Conv2D dBias Arg 3");
                CHECK_CL_ERR(clSetKernelArg(k_bias, 4, sizeof(cl_int), &cmd->out_h), "Conv2D dBias Arg 4");
                CHECK_CL_ERR(clSetKernelArg(k_bias, 5, sizeof(cl_int), &cmd->out_w), "Conv2D dBias Arg 5");
                size_t gws_b[1] = { (size_t)cmd->C_out };
                CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(k_bias, 1, gws_b, NULL, "conv2d_bias_grad"), "Conv2D Bias Grad Enqueue");
            }
            return 1;
        }
        case COMMAND_PATCH_PERMUTE_RESHAPE: {
            PatchPermuteCommandData* cmd = (PatchPermuteCommandData*)data;
            if (!cmd || !cmd->input || !cmd->output) {
                fprintf(stderr, "[C] Submit PatchPermute: Invalid buffers.\n");
                return 0;
            }
            if (cmd->B <= 0 || cmd->C <= 0 || cmd->H <= 0 || cmd->W <= 0) {
                if ((size_t)cmd->B * cmd->C * cmd->H * cmd->W == 0) { return 1; }
                fprintf(stderr, "[C] Submit PatchPermute: Invalid dimensions.\n");
                return 0;
            }
            cl_kernel kernel = patch_permute_kernel_fast ? patch_permute_kernel_fast : patch_permute_kernel;
            if (!kernel) { fprintf(stderr, "[C] Submit PatchPermute: Kernel not compiled.\n"); return 0; }
            cl_mem in_mem = (cl_mem)cmd->input;
            cl_mem out_mem = (cl_mem)cmd->output;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in_mem), "PatchPermute Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &out_mem), "PatchPermute Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->B), "PatchPermute Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->C), "PatchPermute Arg 3");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->H), "PatchPermute Arg 4");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->W), "PatchPermute Arg 5");
            size_t gws[1] = { (size_t)cmd->B * cmd->C * cmd->H * cmd->W };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "patch_permute_reshape"), "PatchPermute Enqueue");
            return 1;
        }
        case COMMAND_PATCH_PERMUTE_RESHAPE_BACKWARD: {
            PatchPermuteCommandData* cmd = (PatchPermuteCommandData*)data;
            if (!cmd || !cmd->input || !cmd->output) {
                fprintf(stderr, "[C] Submit PatchPermute Bwd: Invalid buffers.\n");
                return 0;
            }
            if (cmd->B <= 0 || cmd->C <= 0 || cmd->H <= 0 || cmd->W <= 0) {
                if ((size_t)cmd->B * cmd->C * cmd->H * cmd->W == 0) { return 1; }
                fprintf(stderr, "[C] Submit PatchPermute Bwd: Invalid dimensions.\n");
                return 0;
            }
            cl_kernel kernel = patch_permute_backward_kernel_fast ? patch_permute_backward_kernel_fast : patch_permute_backward_kernel;
            if (!kernel) { fprintf(stderr, "[C] Submit PatchPermute Bwd: Kernel not compiled.\n"); return 0; }
            cl_mem in_mem = (cl_mem)cmd->input;
            cl_mem out_mem = (cl_mem)cmd->output;
            CHECK_CL_ERR(clSetKernelArg(kernel, 0, sizeof(cl_mem), &in_mem), "PatchPermuteBwd Arg 0");
            CHECK_CL_ERR(clSetKernelArg(kernel, 1, sizeof(cl_mem), &out_mem), "PatchPermuteBwd Arg 1");
            CHECK_CL_ERR(clSetKernelArg(kernel, 2, sizeof(cl_int), &cmd->B), "PatchPermuteBwd Arg 2");
            CHECK_CL_ERR(clSetKernelArg(kernel, 3, sizeof(cl_int), &cmd->C), "PatchPermuteBwd Arg 3");
            CHECK_CL_ERR(clSetKernelArg(kernel, 4, sizeof(cl_int), &cmd->H), "PatchPermuteBwd Arg 4");
            CHECK_CL_ERR(clSetKernelArg(kernel, 5, sizeof(cl_int), &cmd->W), "PatchPermuteBwd Arg 5");
            size_t gws[1] = { (size_t)cmd->B * cmd->C * cmd->H * cmd->W };
            CHECK_CL_ERR(ENQUEUE_KERNEL_PROFILED(kernel, 1, gws, NULL, "patch_permute_reshape_backward"), "PatchPermute Bwd Enqueue");
            return 1;
        }

        default:
            fprintf(stderr, "[C] submit_kernel_command: Error - Unknown or unhandled command code: %d\n", command);
            return 0;
    } // end switch

    #undef CHECK_CL_ERR
    fprintf(stderr, "[C] submit_kernel_command: Error - Reached end of switch without successful command submission (Command code: %d).\n", command);
    return 0;
}

/**
 * @brief Blocks until all previously enqueued commands in the OpenCL queue have finished execution.
 */
int finish_queue_and_check(int gpu_index, const char* func_name) {
    cl_command_queue active_queue = queue;
    GpuSlot* slot = cc_get_slot(gpu_index);
    if (slot && slot->queue) {
        active_queue = slot->queue;
    }
    if (!active_queue) {
        cc_set_last_error("[C] %s: Error - Command queue is NULL. Cannot finish.", func_name ? func_name : "finish_queue_and_check");
        fprintf(stderr, "[C] %s: Error - Command queue is NULL. Cannot finish.\n", func_name ? func_name : "finish_queue_and_check");
        return 0;
    }
    cl_int err = clFinish(active_queue);
    if (err != CL_SUCCESS) {
        cc_set_last_error("[C] %s: Error during clFinish after submitting commands: %s (%d)",
                          func_name ? func_name : "finish_queue_and_check", clGetErrorString(err), err);
        fprintf(stderr, "[C] %s: Error during clFinish after submitting commands: %s (%d)\n", func_name ? func_name : "finish_queue_and_check", clGetErrorString(err), err);
        return 0;
    }
    return 1;
}

DLLEXPORT int finish_gpu(int gpu_index) {
    return finish_queue_and_check(gpu_index, "finish_gpu");
}

DLLEXPORT const char* cc_get_last_error(void) {
    return g_last_error_message[0] ? g_last_error_message : "OK";
}

DLLEXPORT const char* cc_get_version(void) {
    return CC_DRIVER_VERSION;
}

// --- Exported Function Definitions (Wrappers for Kernel Execution) ---

DLLEXPORT int execute_matmul_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int B, int M, int N, int K) {
    cc_clear_last_error();
    if (!buffer_a || !buffer_b || !buffer_c) {
        cc_set_last_error("[C] execute_matmul_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_matmul_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (B <= 0 || M <= 0 || N <= 0) {
        if ((size_t)B * M * N == 0) { return 1; }
        cc_set_last_error("[C] execute_matmul_on_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d)", B, M, N);
        fprintf(stderr, "[C] execute_matmul_on_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d).\n", B, M, N);
        return 0;
    }
    if (K <= 0) {
        cc_set_last_error("[C] execute_matmul_on_gpu: Error - Invalid non-positive dimension K=%d", K);
        fprintf(stderr, "[C] execute_matmul_on_gpu: Error - Invalid non-positive dimension K=%d.\n", K);
        return 0;
    }
    BMMCommandData cmd_data = { buffer_a, buffer_b, buffer_c, B, M, N, K };
    if (!submit_kernel_command(gpu_index, COMMAND_MATRIX_MULTIPLY, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_softmax_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int num_rows, int row_size) {
    if (!buffer_input || !buffer_output) { fprintf(stderr, "[C] execute_softmax_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_rows <= 0 || row_size <= 0) { if (num_rows == 0) return 1; fprintf(stderr, "[C] execute_softmax_on_gpu: Error - Invalid non-positive dimensions (rows=%d, size=%d).\n", num_rows, row_size); return 0; }
    SoftmaxCommandData cmd_data = { buffer_input, buffer_output, num_rows, row_size };
    if (!submit_kernel_command(gpu_index, COMMAND_SOFTMAX_ROWWISE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_gelu_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int num_elements) {
    if (!buffer_input || !buffer_output) { fprintf(stderr, "[C] execute_gelu_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_elements <= 0) { if (num_elements == 0) return 1; fprintf(stderr, "[C] execute_gelu_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements); return 0; }
    GeluCommandData cmd_data = { buffer_input, buffer_output, num_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_GELU_ELEMENTWISE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_add_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int num_elements) {
    if (!buffer_a || !buffer_b || !buffer_c) { fprintf(stderr, "[C] execute_add_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_elements <= 0) { if (num_elements == 0) return 1; fprintf(stderr, "[C] execute_add_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements); return 0; }
    AddCommandData cmd_data = { buffer_a, buffer_b, buffer_c, num_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_ADD_ELEMENTWISE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_add_bias_on_gpu(int gpu_index, void* buffer_a_or_c, void* buffer_b_bias, int M, int N) {
    if (!buffer_a_or_c || !buffer_b_bias) { fprintf(stderr, "[C] execute_add_bias_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (M <= 0 || N <= 0) { if ((size_t)M * N == 0) return 1; fprintf(stderr, "[C] execute_add_bias_on_gpu: Error - Invalid non-positive dimensions (M=%d, N=%d).\n", M, N); return 0; }
    AddBiasMNCommandData cmd_data = { buffer_a_or_c, buffer_b_bias, M, N };
    if (!submit_kernel_command(gpu_index, COMMAND_ADD_BIAS_MN, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_mul_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int num_elements) {
    if (!buffer_a || !buffer_b || !buffer_c) { fprintf(stderr, "[C] execute_mul_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_elements <= 0) { if (num_elements == 0) return 1; fprintf(stderr, "[C] execute_mul_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements); return 0; }
    MulCommandData cmd_data = { buffer_a, buffer_b, buffer_c, num_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_MUL_ELEMENTWISE, &cmd_data)) { return 0; }
    return 1;
}

static cl_command_queue cc_get_slot_queue(int gpu_index, int prefer_transfer, GpuSlot** out_slot) {
    if (out_slot) { *out_slot = NULL; }
    GpuSlot* slot = cc_get_slot(gpu_index);
    if (out_slot) { *out_slot = slot; }
    if (!slot) {
        return queue;
    }
    if (prefer_transfer && slot->transfer_queue) {
        return slot->transfer_queue;
    }
    if (slot->queue) {
        return slot->queue;
    }
    return queue;
}

DLLEXPORT int execute_conv2d_forward_on_gpu(
    int gpu_index,
    void* input,
    void* weights,
    void* bias,
    void* output,
    int B,
    int C_in,
    int H,
    int W,
    int C_out,
    int K_h,
    int K_w,
    int stride_h,
    int stride_w
) {
    cc_clear_last_error();
    if (!input || !weights || !output) {
        cc_set_last_error("[C] execute_conv2d_forward_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_conv2d_forward_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (B <= 0 || C_in <= 0 || H <= 0 || W <= 0 || C_out <= 0 || K_h <= 0 || K_w <= 0 || stride_h <= 0 || stride_w <= 0) {
        if ((size_t)B * C_out == 0) { return 1; }
        cc_set_last_error("[C] execute_conv2d_forward_on_gpu: Error - Invalid dimensions (B=%d, Cin=%d, H=%d, W=%d, Cout=%d, Kh=%d, Kw=%d, Sh=%d, Sw=%d)",
                          B, C_in, H, W, C_out, K_h, K_w, stride_h, stride_w);
        fprintf(stderr, "[C] execute_conv2d_forward_on_gpu: Error - Invalid dimensions (B=%d, Cin=%d, H=%d, W=%d, Cout=%d, Kh=%d, Kw=%d, Sh=%d, Sw=%d).\n",
                B, C_in, H, W, C_out, K_h, K_w, stride_h, stride_w);
        return 0;
    }
    int out_h = (H - K_h) / stride_h + 1;
    int out_w = (W - K_w) / stride_w + 1;
    if (out_h <= 0 || out_w <= 0) {
        cc_set_last_error("[C] execute_conv2d_forward_on_gpu: Error - Output dimensions non-positive (out_h=%d, out_w=%d)", out_h, out_w);
        fprintf(stderr, "[C] execute_conv2d_forward_on_gpu: Error - Output dimensions non-positive (out_h=%d, out_w=%d).\n", out_h, out_w);
        return 0;
    }
    Conv2DForwardCommandData cmd = { input, weights, bias, output, B, C_in, H, W, C_out, K_h, K_w, stride_h, stride_w, out_h, out_w };
    if (!submit_kernel_command(gpu_index, COMMAND_CONV2D_FORWARD, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_conv2d_backward_on_gpu(
    int gpu_index,
    void* grad_output,
    void* input,
    void* weights,
    void* grad_input,
    void* grad_weights,
    void* grad_bias,
    int B,
    int C_in,
    int H,
    int W,
    int C_out,
    int K_h,
    int K_w,
    int stride_h,
    int stride_w
) {
    cc_clear_last_error();
    if (!grad_output || !input || !weights) {
        cc_set_last_error("[C] execute_conv2d_backward_on_gpu: Error - NULL required buffer provided");
        fprintf(stderr, "[C] execute_conv2d_backward_on_gpu: Error - NULL required buffer provided.\n");
        return 0;
    }
    if (!grad_input && !grad_weights && !grad_bias) { return 1; }
    if (B <= 0 || C_in <= 0 || H <= 0 || W <= 0 || C_out <= 0 || K_h <= 0 || K_w <= 0 || stride_h <= 0 || stride_w <= 0) {
        if ((size_t)B * C_out == 0) { return 1; }
        cc_set_last_error("[C] execute_conv2d_backward_on_gpu: Error - Invalid dimensions (B=%d, Cin=%d, H=%d, W=%d, Cout=%d, Kh=%d, Kw=%d, Sh=%d, Sw=%d)",
                          B, C_in, H, W, C_out, K_h, K_w, stride_h, stride_w);
        fprintf(stderr, "[C] execute_conv2d_backward_on_gpu: Error - Invalid dimensions (B=%d, Cin=%d, H=%d, W=%d, Cout=%d, Kh=%d, Kw=%d, Sh=%d, Sw=%d).\n",
                B, C_in, H, W, C_out, K_h, K_w, stride_h, stride_w);
        return 0;
    }
    int out_h = (H - K_h) / stride_h + 1;
    int out_w = (W - K_w) / stride_w + 1;
    if (out_h <= 0 || out_w <= 0) {
        cc_set_last_error("[C] execute_conv2d_backward_on_gpu: Error - Output dimensions non-positive (out_h=%d, out_w=%d)", out_h, out_w);
        fprintf(stderr, "[C] execute_conv2d_backward_on_gpu: Error - Output dimensions non-positive (out_h=%d, out_w=%d).\n", out_h, out_w);
        return 0;
    }
    Conv2DBackwardCommandData cmd = { grad_output, input, weights, grad_input, grad_weights, grad_bias,
                                      B, C_in, H, W, C_out, K_h, K_w, stride_h, stride_w, out_h, out_w };
    if (!submit_kernel_command(gpu_index, COMMAND_CONV2D_BACKWARD, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_patch_permute_reshape_on_gpu(int gpu_index, void* input, void* output, int B, int C, int H, int W) {
    cc_clear_last_error();
    if (!input || !output) {
        cc_set_last_error("[C] execute_patch_permute_reshape_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_patch_permute_reshape_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (B <= 0 || C <= 0 || H <= 0 || W <= 0) {
        if ((size_t)B * C * H * W == 0) { return 1; }
        cc_set_last_error("[C] execute_patch_permute_reshape_on_gpu: Error - Invalid dimensions (B=%d, C=%d, H=%d, W=%d)", B, C, H, W);
        fprintf(stderr, "[C] execute_patch_permute_reshape_on_gpu: Error - Invalid dimensions (B=%d, C=%d, H=%d, W=%d).\n", B, C, H, W);
        return 0;
    }
    PatchPermuteCommandData cmd = { input, output, B, C, H, W };
    if (!submit_kernel_command(gpu_index, COMMAND_PATCH_PERMUTE_RESHAPE, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_patch_permute_reshape_backward_on_gpu(int gpu_index, void* grad_tokens, void* grad_feature, int B, int C, int H, int W) {
    cc_clear_last_error();
    if (!grad_tokens || !grad_feature) {
        cc_set_last_error("[C] execute_patch_permute_reshape_backward_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_patch_permute_reshape_backward_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (B <= 0 || C <= 0 || H <= 0 || W <= 0) {
        if ((size_t)B * C * H * W == 0) { return 1; }
        cc_set_last_error("[C] execute_patch_permute_reshape_backward_on_gpu: Error - Invalid dimensions (B=%d, C=%d, H=%d, W=%d)", B, C, H, W);
        fprintf(stderr, "[C] execute_patch_permute_reshape_backward_on_gpu: Error - Invalid dimensions (B=%d, C=%d, H=%d, W=%d).\n", B, C, H, W);
        return 0;
    }
    PatchPermuteCommandData cmd = { grad_tokens, grad_feature, B, C, H, W };
    if (!submit_kernel_command(gpu_index, COMMAND_PATCH_PERMUTE_RESHAPE_BACKWARD, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_eon_encoder_chain_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, size_t num_bytes) {
    cc_clear_last_error();
    if (!buffer_input || !buffer_output) {
        cc_set_last_error("[C] execute_eon_encoder_chain_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_eon_encoder_chain_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (num_bytes == 0) { return 1; }
    CloneCommandData cmd = { buffer_input, buffer_output, num_bytes };
    if (!submit_kernel_command(gpu_index, COMMAND_CLONE, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_eon_encoder_backward_chain_on_gpu(int gpu_index, void* buffer_grad_output, void* buffer_grad_input, size_t num_bytes) {
    cc_clear_last_error();
    if (!buffer_grad_output || !buffer_grad_input) {
        cc_set_last_error("[C] execute_eon_encoder_backward_chain_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_eon_encoder_backward_chain_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (num_bytes == 0) { return 1; }
    CloneCommandData cmd = { buffer_grad_output, buffer_grad_input, num_bytes };
    if (!submit_kernel_command(gpu_index, COMMAND_CLONE, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int compute_ctc_loss_cpu(
    const float* logits,
    int T,
    int B,
    int V,
    const int* targets,
    int max_target_len,
    const int* target_lengths,
    const int* input_lengths,
    int blank_index,
    float* loss_out,
    float* grad_out
) {
    cc_clear_last_error();
    if (!logits || !targets || !target_lengths || !input_lengths || !loss_out) {
        cc_set_last_error("[C] compute_ctc_loss_cpu: Error - NULL required pointer provided");
        fprintf(stderr, "[C] compute_ctc_loss_cpu: Error - NULL required pointer provided.\n");
        return 0;
    }
    if (T <= 0 || B <= 0 || V <= 0 || max_target_len < 0) {
        cc_set_last_error("[C] compute_ctc_loss_cpu: Error - Invalid dimensions (T=%d, B=%d, V=%d, max_target_len=%d)", T, B, V, max_target_len);
        fprintf(stderr, "[C] compute_ctc_loss_cpu: Error - Invalid dimensions (T=%d, B=%d, V=%d, max_target_len=%d).\n", T, B, V, max_target_len);
        return 0;
    }
    if (blank_index < 0 || blank_index >= V) {
        cc_set_last_error("[C] compute_ctc_loss_cpu: Error - Invalid blank index %d for vocab size %d", blank_index, V);
        fprintf(stderr, "[C] compute_ctc_loss_cpu: Error - Invalid blank index %d for vocab size %d.\n", blank_index, V);
        return 0;
    }

    const float log_zero = -1e30f;
    size_t total_elements = (size_t)T * (size_t)V;
    float* log_probs = (float*)malloc(total_elements * sizeof(float));
    float* probs = (float*)malloc(total_elements * sizeof(float));
    if (!log_probs || !probs) {
        cc_set_last_error("[C] compute_ctc_loss_cpu: Error - Failed to allocate intermediate buffers");
        fprintf(stderr, "[C] compute_ctc_loss_cpu: Error - Failed to allocate intermediate buffers.\n");
        free(log_probs);
        free(probs);
        return 0;
    }
    if (grad_out) {
        memset(grad_out, 0, (size_t)B * total_elements * sizeof(float));
    }

    for (int b = 0; b < B; ++b) {
        int T_b = input_lengths[b];
        int L_b = target_lengths[b];
        if (T_b <= 0 || T_b > T) {
            fprintf(stderr, "[C] compute_ctc_loss_cpu: Warning - Adjusting invalid input length %d for batch %d.\n", T_b, b);
            T_b = T;
        }
        if (L_b < 0 || L_b > max_target_len) {
            fprintf(stderr, "[C] compute_ctc_loss_cpu: Warning - Adjusting invalid target length %d for batch %d.\n", L_b, b);
            L_b = (L_b < 0) ? 0 : max_target_len;
        }
        const float* logits_b = logits + (size_t)b * total_elements;
        float* log_probs_b = log_probs;
        float* probs_b = probs;

        for (int t = 0; t < T; ++t) {
            const float* logits_t = logits_b + (size_t)t * V;
            float* log_probs_t = log_probs_b + (size_t)t * V;
            float* probs_t = probs_b + (size_t)t * V;
            float max_logit = logits_t[0];
            for (int k = 1; k < V; ++k) {
                if (logits_t[k] > max_logit) { max_logit = logits_t[k]; }
            }
            float denom = 0.0f;
            for (int k = 0; k < V; ++k) {
                float val = expf(logits_t[k] - max_logit);
                denom += val;
                probs_t[k] = val;
            }
            float log_denom = max_logit + logf(denom);
            for (int k = 0; k < V; ++k) {
                probs_t[k] /= denom;
                log_probs_t[k] = logits_t[k] - log_denom;
            }
        }

        if (T_b == 0) {
            loss_out[b] = 0.0f;
            continue;
        }

        int S = 2 * L_b + 1;
        if (S <= 0) { S = 1; }
        int* ext = (int*)malloc((size_t)S * sizeof(int));
        float* alpha = (float*)malloc((size_t)T_b * S * sizeof(float));
        float* beta = (float*)malloc((size_t)T_b * S * sizeof(float));
        if (!ext || !alpha || !beta) {
            cc_set_last_error("[C] compute_ctc_loss_cpu: Error - Failed to allocate DP buffers");
            fprintf(stderr, "[C] compute_ctc_loss_cpu: Error - Failed to allocate DP buffers.\n");
            free(ext); free(alpha); free(beta);
            free(log_probs); free(probs);
            return 0;
        }

        for (int i = 0; i < S; ++i) { ext[i] = blank_index; }
        for (int l = 0; l < L_b; ++l) { ext[2 * l + 1] = targets[b * max_target_len + l]; }

        for (int i = 0; i < T_b * S; ++i) { alpha[i] = beta[i] = log_zero; }
        const float* log_probs_bt0 = log_probs_b;
        alpha[0 * S + 0] = log_probs_bt0[blank_index];
        if (S > 1) {
            int sym = ext[1];
            if (sym >= 0 && sym < V) { alpha[0 * S + 1] = log_probs_bt0[sym]; }
        }

        for (int t = 1; t < T_b; ++t) {
            const float* log_probs_t = log_probs_b + (size_t)t * V;
            for (int s = 0; s < S; ++s) {
                int sym = ext[s];
                float sum = alpha[(size_t)(t - 1) * S + s];
                if (s - 1 >= 0) {
                    float alt = alpha[(size_t)(t - 1) * S + (s - 1)];
                    sum = cc_log_sum_exp_pair(sum, alt);
                }
                if (s - 2 >= 0 && sym != blank_index && ext[s] != ext[s - 2]) {
                    float alt = alpha[(size_t)(t - 1) * S + (s - 2)];
                    sum = cc_log_sum_exp_pair(sum, alt);
                }
                if (sym >= 0 && sym < V) {
                    alpha[(size_t)t * S + s] = sum + log_probs_t[sym];
                } else {
                    alpha[(size_t)t * S + s] = log_zero;
                }
            }
        }

        beta[(size_t)(T_b - 1) * S + (S - 1)] = 0.0f;
        if (S > 1) { beta[(size_t)(T_b - 1) * S + (S - 2)] = 0.0f; }

        for (int t = T_b - 2; t >= 0; --t) {
            const float* log_probs_next = log_probs_b + (size_t)(t + 1) * V;
            for (int s = 0; s < S; ++s) {
                int sym = ext[s];
                float sum = log_zero;
                if (sym >= 0 && sym < V) {
                    float stay = beta[(size_t)(t + 1) * S + s];
                    if (stay != log_zero) { sum = stay + log_probs_next[sym]; }
                }
                if (s + 1 < S) {
                    int sym1 = ext[s + 1];
                    float b1 = beta[(size_t)(t + 1) * S + (s + 1)];
                    if (b1 != log_zero && sym1 >= 0 && sym1 < V) {
                        float cand = b1 + log_probs_next[sym1];
                        sum = cc_log_sum_exp_pair(sum, cand);
                    }
                }
                if (s + 2 < S && sym != blank_index && ext[s + 2] != sym) {
                    int sym2 = ext[s + 2];
                    float b2 = beta[(size_t)(t + 1) * S + (s + 2)];
                    if (b2 != log_zero && sym2 >= 0 && sym2 < V) {
                        float cand = b2 + log_probs_next[sym2];
                        sum = cc_log_sum_exp_pair(sum, cand);
                    }
                }
                beta[(size_t)t * S + s] = sum;
            }
        }

        float log_likelihood = alpha[(size_t)(T_b - 1) * S + (S - 1)];
        if (S > 1) { log_likelihood = cc_log_sum_exp_pair(log_likelihood, alpha[(size_t)(T_b - 1) * S + (S - 2)]); }
        loss_out[b] = -log_likelihood;

        if (grad_out) {
            float* grad_b = grad_out + (size_t)b * total_elements;
            for (int t = 0; t < T; ++t) {
                memset(grad_b + (size_t)t * V, 0, (size_t)V * sizeof(float));
            }
            for (int t = 0; t < T_b; ++t) {
                float* posterior = (float*)calloc((size_t)V, sizeof(float));
                if (!posterior) {
                    fprintf(stderr, "[C] compute_ctc_loss_cpu: Error - Failed to allocate posterior buffer.\n");
                    free(ext); free(alpha); free(beta);
                    free(log_probs); free(probs);
                    return 0;
                }
                for (int s = 0; s < S; ++s) {
                    int sym = ext[s];
                    if (sym < 0 || sym >= V) { continue; }
                    float log_post = alpha[(size_t)t * S + s] + beta[(size_t)t * S + s] - log_likelihood;
                    posterior[sym] += expf(log_post);
                }
                const float* prob_t = probs_b + (size_t)t * V;
                float* grad_t = grad_b + (size_t)t * V;
                for (int k = 0; k < V; ++k) {
                    grad_t[k] = prob_t[k] - posterior[k];
                }
                free(posterior);
            }
        }

        free(ext);
        free(alpha);
        free(beta);
    }

    free(log_probs);
    free(probs);
    return 1;
}

DLLEXPORT int execute_layernorm_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int num_rows, int row_size, float eps) {
    if (!buffer_input || !buffer_output) { fprintf(stderr, "[C] execute_layernorm_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_rows <= 0 || row_size <= 0) { if (num_rows == 0) return 1; fprintf(stderr, "[C] execute_layernorm_on_gpu: Error - Invalid non-positive dimensions (rows=%d, size=%d).\n", num_rows, row_size); return 0; }
    float effective_eps = (eps > 0) ? eps : 1e-5f;
    LayerNormCommandData cmd_data = { buffer_input, buffer_output, num_rows, row_size, effective_eps };
    if (!submit_kernel_command(gpu_index, COMMAND_LAYER_NORM, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_clone_on_gpu(int gpu_index, void* src_buffer, void* dst_buffer, size_t size) {
    if (!src_buffer || !dst_buffer) { fprintf(stderr, "[C] execute_clone_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (size == 0) return 1;
    CloneCommandData cmd_data = { src_buffer, dst_buffer, size };
    if (!submit_kernel_command(gpu_index, COMMAND_CLONE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_transpose_on_gpu(int gpu_index, void* buffer_input, void* buffer_output, int rows, int cols) {
    if (!buffer_input || !buffer_output) { fprintf(stderr, "[C] execute_transpose_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (rows <= 0 || cols <= 0) { if ((size_t)rows * cols == 0) return 1; fprintf(stderr, "[C] execute_transpose_on_gpu: Error - Invalid non-positive dimensions (rows=%d, cols=%d).\n", rows, cols); return 0; }
    TransposeCommandData cmd_data = { buffer_input, buffer_output, rows, cols };
    if (!submit_kernel_command(gpu_index, COMMAND_TRANSPOSE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_gelu_backward_on_gpu(int gpu_index, void* buffer_input, void* buffer_grad_output, void* buffer_grad_input, int num_elements) {
    if (!buffer_input || !buffer_grad_output || !buffer_grad_input) { fprintf(stderr, "[C] execute_gelu_backward_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_elements <= 0) { if (num_elements == 0) return 1; fprintf(stderr, "[C] execute_gelu_backward_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements); return 0; }
    GeluBackwardCommandData cmd_data = { buffer_input, buffer_grad_output, buffer_grad_input, num_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_GELU_BACKWARD_ELEMENTWISE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_matmul_backward_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_dc, void* buffer_da, void* buffer_db, int B, int M, int N, int K) {
    if (!buffer_a || !buffer_b || !buffer_dc) { fprintf(stderr, "[C] execute_matmul_backward_on_gpu: Error - NULL required input buffer handle provided (A, B, or dC).\n"); return 0; }
    if (!buffer_da && !buffer_db) { return 1; }
    int need_da = (buffer_da != NULL);
    int need_db = (buffer_db != NULL);
    if (B <= 0 || M <= 0 || N <= 0 || K <= 0) {
        int da_zero = need_da && ((size_t)B*M*K == 0);
        int db_zero = need_db && ((size_t)K*N == 0);
        if(need_da && need_db && (da_zero || db_zero)) { }
        else if (need_da && da_zero && !need_db) { }
        else if (need_db && db_zero && !need_da) { }
        else if (!need_da && !need_db) { return 1; }
        else {
            fprintf(stderr, "[C] execute_matmul_backward_on_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d, K=%d) for requested gradient.\n", B, M, N, K);
            return 0;
        }
    }
    MatMulBackwardData cmd_data = { buffer_a, buffer_b, buffer_dc, buffer_da, buffer_db, B, M, N, K };
    int success = 1;
    if (need_da && (size_t)B * M * K > 0) {
        if (!submit_kernel_command(gpu_index, COMMAND_MATMUL_BACKWARD_DA, &cmd_data)) { success = 0; }
    }
    if (need_db && (size_t)K * N > 0) {
        if (!submit_kernel_command(gpu_index, COMMAND_MATMUL_BACKWARD_DB, &cmd_data)) { success = 0; }
    }
    return success;
}
DLLEXPORT int execute_layernorm_backward_on_gpu(int gpu_index, void* buffer_dy, void* buffer_x, void* buffer_dx, int num_rows, int row_size, float eps) {
    if (!buffer_dy || !buffer_x || !buffer_dx) { fprintf(stderr, "[C] execute_layernorm_backward_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_rows <= 0 || row_size <= 0) { if (num_rows == 0) return 1; fprintf(stderr, "[C] execute_layernorm_backward_on_gpu: Error - Invalid non-positive dimensions (rows=%d, size=%d).\n", num_rows, row_size); return 0; }
    float effective_eps = (eps > 0) ? eps : 1e-5f;
    LayerNormBackwardCommandData cmd_data = { buffer_dy, buffer_x, buffer_dx, num_rows, row_size, effective_eps };
    if (!submit_kernel_command(gpu_index, COMMAND_LAYER_NORM_BACKWARD, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_adam_update_on_gpu(int gpu_index, void* param_buffer, void* grad_buffer, void* m_buffer, void* v_buffer, int num_elements, int t, float lr, float beta1, float beta2, float eps, float weight_decay) {
    cc_clear_last_error();
    if (!param_buffer || !grad_buffer || !m_buffer || !v_buffer) {
        cc_set_last_error("[C] execute_adam_update_on_gpu: Error - NULL buffer handle provided");
        fprintf(stderr, "[C] execute_adam_update_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (num_elements <= 0) {
        if (num_elements == 0) { return 1; }
        cc_set_last_error("[C] execute_adam_update_on_gpu: Error - Invalid non-positive number of elements (%d)", num_elements);
        fprintf(stderr, "[C] execute_adam_update_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements);
        return 0;
    }
    if (t <= 0 || lr < 0.0f || beta1 < 0.0f || beta1 >= 1.0f || beta2 < 0.0f || beta2 >= 1.0f || eps < 0.0f || weight_decay < 0.0f) {
         cc_set_last_error("[C] execute_adam_update_on_gpu: Error - Invalid hyperparameters (t=%d, lr=%f, b1=%f, b2=%f, eps=%f, wd=%f)",
                           t, lr, beta1, beta2, eps, weight_decay);
         fprintf(stderr, "[C] execute_adam_update_on_gpu: Error - Invalid hyperparameters (t=%d, lr=%f, b1=%f, b2=%f, eps=%f, wd=%f).\n", t, lr, beta1, beta2, eps, weight_decay);
         return 0;
    }
    float beta1_t = (float)pow((double)beta1, (double)t);
    float beta2_t = (float)pow((double)beta2, (double)t);
    AdamCommandData cmd_data = { param_buffer, grad_buffer, m_buffer, v_buffer, num_elements, t, lr, beta1, beta2, eps, weight_decay, beta1_t, beta2_t };
    if (!submit_kernel_command(gpu_index, COMMAND_ADAM_UPDATE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_softmax_backward_on_gpu(int gpu_index, void* buffer_dy, void* buffer_y, void* buffer_dx, int num_rows, int row_size) {
    if (!buffer_dy || !buffer_y || !buffer_dx) { fprintf(stderr, "[C] execute_softmax_backward_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_rows <= 0 || row_size <= 0) { if (num_rows == 0) return 1; fprintf(stderr, "[C] execute_softmax_backward_on_gpu: Error - Invalid non-positive dimensions (rows=%d, size=%d).\n", num_rows, row_size); return 0; }
    SoftmaxBackwardCommandData cmd_data = { buffer_dy, buffer_y, buffer_dx, num_rows, row_size };
    if (!submit_kernel_command(gpu_index, COMMAND_SOFTMAX_BACKWARD, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_mul_backward_on_gpu(int gpu_index, void* buffer_dC, void* buffer_A, void* buffer_B, void* buffer_dA, void* buffer_dB, int num_elements) {
    if (!buffer_dC || !buffer_A || !buffer_B) { fprintf(stderr, "[C] execute_mul_backward_on_gpu: Error - NULL required input buffer handle provided (dC, A, or B).\n"); return 0; }
    if (!buffer_dA && !buffer_dB) { return 1; }
    if (num_elements <= 0) { if (num_elements == 0) return 1; fprintf(stderr, "[C] execute_mul_backward_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements); return 0; }
    MulBackwardCommandData cmd_data = { buffer_dC, buffer_A, buffer_B, buffer_dA, buffer_dB, num_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_MUL_BACKWARD, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_transpose_backward_on_gpu(int gpu_index, void* buffer_dC, void* buffer_dA, int rows_A, int cols_A) {
    if (!buffer_dC || !buffer_dA) { fprintf(stderr, "[C] execute_transpose_backward_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (rows_A <= 0 || cols_A <= 0) { if ((size_t)rows_A * cols_A == 0) return 1; fprintf(stderr, "[C] execute_transpose_backward_on_gpu: Error - Invalid non-positive dimensions (rows_A=%d, cols_A=%d).\n", rows_A, cols_A); return 0; }
    TransposeBackwardCommandData cmd_data = { buffer_dC, buffer_dA, rows_A, cols_A };
    if (!submit_kernel_command(gpu_index, COMMAND_TRANSPOSE_BACKWARD, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_embedding_lookup_gpu(int gpu_index, void* idx, void* w, void* o, int b, int s, int d, int v) {
    if (!idx || !w || !o) { fprintf(stderr, "[C] execute_embedding_lookup_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (b <= 0 || s <= 0) { if ((size_t)b * s == 0) return 1; fprintf(stderr, "[C] execute_embedding_lookup_gpu: Error - Invalid non-positive dimensions (b=%d, s=%d).\n", b, s); return 0; }
    if (d <= 0 || v <= 0) { fprintf(stderr, "[C] execute_embedding_lookup_gpu: Error - Invalid non-positive dimensions (d=%d, v=%d).\n", d, v); return 0; }
    EmbeddingLookupCommandData cd = { idx, w, o, b, s, d, v };
    if (!submit_kernel_command(gpu_index, COMMAND_EMBEDDING_LOOKUP, &cd)) { return 0; }
    return 1;
}
DLLEXPORT int execute_embedding_backward_gpu(int gpu_index, void* d_o, void* idx, void* d_w, int b, int s, int d, int v) {
    size_t num_grad_elements;
    void* delta_dw_buffer = NULL;
    size_t delta_dw_size_bytes;
    int success = 1;

    if (!d_o || !idx || !d_w) { fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (b <= 0 || s <= 0) { if ((size_t)b * s == 0) return 1; fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Invalid non-positive dimensions (b=%d, s=%d).\n", b, s); return 0; }
    if (d <= 0 || v <= 0) { if ((size_t)v * d == 0) return 1; fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Invalid non-positive dimensions (d=%d, v=%d).\n", d, v); return 0; }
    if (!embedding_backward_calc_delta_local_kernel || !add_kernel) { fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Required kernels not compiled/available.\n"); return 0; }

    num_grad_elements = (size_t)v * d;
    delta_dw_size_bytes = num_grad_elements * sizeof(FP_TYPE);

    delta_dw_buffer = allocate_gpu_memory(gpu_index, delta_dw_size_bytes);
    if (!delta_dw_buffer) { fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Failed to allocate temporary delta_dw buffer.\n"); return 0; }

    if (!zero_gpu_buffer(gpu_index, delta_dw_buffer, delta_dw_size_bytes)) {
        fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Failed to zero temporary delta_dw buffer.\n");
        free_gpu_memory(gpu_index, delta_dw_buffer);
        return 0;
    }

    EmbeddingBackwardPass1CommandData pass1_cd = { d_o, idx, delta_dw_buffer, b, s, d, v };
    if (!submit_kernel_command(gpu_index, COMMAND_EMBEDDING_BACKWARD_PASS1, &pass1_cd)) {
        fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Failed submitting Pass 1 (delta calculation).\n");
        free_gpu_memory(gpu_index, delta_dw_buffer);
        return 0;
    }

    AddCommandData pass2_cd = { d_w, delta_dw_buffer, d_w, (int)num_grad_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_ADD_ELEMENTWISE, &pass2_cd)) {
        fprintf(stderr, "[C] execute_embedding_backward_gpu: Error - Failed submitting Pass 2 (gradient accumulation).\n");
        success = 0;
    }

    free_gpu_memory(gpu_index, delta_dw_buffer);
    return success;
}
DLLEXPORT int execute_reduce_sum_gpu(int gpu_index, void* in, void* out, int B, int M, int N) {
    if (!in || !out) { fprintf(stderr, "[C] execute_reduce_sum_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B <= 0 || M <= 0 || N <= 0) { if ((size_t)B * M == 0 || N == 0) return 1; fprintf(stderr, "[C] execute_reduce_sum_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d).\n", B, M, N); return 0; }
    ReduceSumCommandData cd = { in, out, B, M, N };
    if (!submit_kernel_command(gpu_index, COMMAND_REDUCE_SUM_AXIS01, &cd)) { return 0; }
    return 1;
}
DLLEXPORT int execute_broadcast_add_gpu(int gpu_index, void* a, void* b, void* c, int B, int M, int N) {
    if (!a || !b || !c) { fprintf(stderr, "[C] execute_broadcast_add_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B <= 0 || M <= 0 || N <= 0) { if ((size_t)B * M * N == 0) return 1; fprintf(stderr, "[C] execute_broadcast_add_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d).\n", B, M, N); return 0; }
    BroadcastAddCommandData cd = { a, b, c, B, M, N };
    if (!submit_kernel_command(gpu_index, COMMAND_BROADCAST_ADD_BIAS, &cd)) { return 0; }
    return 1;
}
DLLEXPORT int execute_transpose_batched_gpu(int gpu_index, void* in, void* out, int B_flat, int d1, int d2) {
    if (!in || !out) { fprintf(stderr, "[C] execute_transpose_batched_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B_flat <= 0 || d1 <= 0 || d2 <= 0) { if ((size_t)B_flat * d1 * d2 == 0) return 1; fprintf(stderr, "[C] execute_transpose_batched_gpu: Error - Invalid non-positive dimensions (B_flat=%d, d1=%d, d2=%d).\n", B_flat, d1, d2); return 0; }
    TransposeBatchedCommandData cd = { in, out, B_flat, d1, d2 };
    if (!submit_kernel_command(gpu_index, COMMAND_TRANSPOSE_BATCHED, &cd)) { return 0; }
    return 1;
}
DLLEXPORT int execute_transpose_12_batched_gpu(int gpu_index, void* buffer_in, void* buffer_out, int B, int D1, int D2, int D3) {
    if (!buffer_in || !buffer_out) { fprintf(stderr, "[C] execute_transpose_12_batched_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B <= 0 || D1 <= 0 || D2 <= 0 || D3 <= 0) { if ((size_t)B * D1 * D2 * D3 == 0) return 1; fprintf(stderr, "[C] execute_transpose_12_batched_gpu: Error - Invalid non-positive dimensions (B=%d, D1=%d, D2=%d, D3=%d).\n", B, D1, D2, D3); return 0; }
    Transpose12BatchedCommandData cmd_data = { buffer_in, buffer_out, B, D1, D2, D3 };
    if (!submit_kernel_command(gpu_index, COMMAND_TRANSPOSE_12_BATCHED, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_matmul_batched_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_c, int B, int M, int N, int K) {
    if (!buffer_a || !buffer_b || !buffer_c) { fprintf(stderr, "[C] execute_matmul_batched_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B <= 0 || M <= 0 || N <= 0) { if ((size_t)B * M * N == 0) return 1; fprintf(stderr, "[C] execute_matmul_batched_on_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d).\n", B, M, N); return 0; }
    if (K <= 0) { fprintf(stderr, "[C] execute_matmul_batched_on_gpu: Error - Invalid non-positive dimension K=%d.\n", K); return 0; }
    BMMBatchedCommandData cmd_data = { buffer_a, buffer_b, buffer_c, B, M, N, K };
    if (!submit_kernel_command(gpu_index, COMMAND_MATRIX_MULTIPLY_BATCHED, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_matmul_batched_backward_on_gpu(int gpu_index, void* buffer_a, void* buffer_b, void* buffer_dc, void* buffer_da, void* buffer_db, int B, int M, int N, int K) {
    if (!buffer_a || !buffer_b || !buffer_dc ) { fprintf(stderr, "[C] execute_matmul_batched_backward_on_gpu: Error - NULL required input buffer handle provided (A, B, or dC).\n"); return 0; }
    if (!buffer_da && !buffer_db) { return 1; }
    int need_da = (buffer_da != NULL);
    int need_db = (buffer_db != NULL);
     if (B <= 0 || M <= 0 || N <= 0 || K <= 0) {
        int da_zero = need_da && ((size_t)B*M*K == 0);
        int db_zero = need_db && ((size_t)B*K*N == 0);
        if(need_da && need_db && (da_zero || db_zero)) {}
        else if (need_da && da_zero && !need_db) {}
        else if (need_db && db_zero && !need_da) {}
        else if (!need_da && !need_db) { return 1; }
        else {
            fprintf(stderr, "[C] execute_matmul_batched_backward_on_gpu: Error - Invalid non-positive dimensions (B=%d, M=%d, N=%d, K=%d) for requested gradient.\n", B, M, N, K);
            return 0;
        }
    }
    BMMBatchedBackwardData cmd_data = { buffer_a, buffer_b, buffer_dc, buffer_da, buffer_db, B, M, N, K };
    int success = 1;
    if (need_da && (size_t)B * M * K > 0) {
        if (!submit_kernel_command(gpu_index, COMMAND_MATRIX_MULTIPLY_BATCHED_BACKWARD_DA, &cmd_data)) { success = 0; }
    }
    if (need_db && (size_t)B * K * N > 0) {
        if (!submit_kernel_command(gpu_index, COMMAND_MATRIX_MULTIPLY_BATCHED_BACKWARD_DB, &cmd_data)) { success = 0; }
    }
    return success;
}
DLLEXPORT int execute_log_softmax_stable_gpu(int gpu_index, void* input_logits, void* output_log_probs, int B_S_rows, int V_cols) {
    if (!input_logits || !output_log_probs) { fprintf(stderr, "[C] execute_log_softmax_stable_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B_S_rows <= 0 || V_cols <= 0) {
        if (B_S_rows == 0) return 1;
        fprintf(stderr, "[C] execute_log_softmax_stable_gpu: Error - Invalid non-positive dimensions (B_S_rows=%d, V_cols=%d).\n", B_S_rows, V_cols); return 0;
    }
    LogSoftmaxStableCommandData cmd_data = { input_logits, output_log_probs, B_S_rows, V_cols };
    if (!submit_kernel_command(gpu_index, COMMAND_LOG_SOFTMAX_STABLE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_cross_entropy_loss_grad_gpu(int gpu_index, void* log_probs, void* target_indices, void* grad_input, void* loss_per_sample, int num_rows, int V) {
    if (!log_probs || !target_indices || !grad_input || !loss_per_sample) { fprintf(stderr, "[C] execute_cross_entropy_loss_grad_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_rows <= 0 || V <= 0) { if (num_rows == 0) return 1; fprintf(stderr, "[C] execute_cross_entropy_loss_grad_gpu: Error - Invalid non-positive dimensions (num_rows=%d, V=%d).\n", num_rows, V); return 0; }
    CrossEntropyLossGradCommandData cmd_data = { log_probs, target_indices, grad_input, loss_per_sample, num_rows, V };
    if (!submit_kernel_command(gpu_index, COMMAND_CROSS_ENTROPY_LOSS_GRAD, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_add_broadcast_pe_gpu(int gpu_index, void* input, void* pe_slice, void* output, int B, int S, int E) {
    if (!input || !pe_slice || !output) { fprintf(stderr, "[C] execute_add_broadcast_pe_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B <= 0 || S <= 0 || E <= 0) { if ((size_t)B * S * E == 0) return 1; fprintf(stderr, "[C] execute_add_broadcast_pe_gpu: Error - Invalid non-positive dimensions (B=%d, S=%d, E=%d).\n", B, S, E); return 0; }
    AddBroadcastPECommandData cmd_data = { input, pe_slice, output, B, S, E };
    if (!submit_kernel_command(gpu_index, COMMAND_ADD_BROADCAST_PE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_hebbian_update_on_gpu(int gpu_index, void* buffer_a, void* buffer_c, void* buffer_w, float learning_rate, int B, int M, int N, int K) {
    if (!buffer_a || !buffer_c || !buffer_w) { fprintf(stderr, "[C] execute_hebbian_update_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (K <= 0 || N <= 0) { if ((size_t)K*N == 0) return 1; fprintf(stderr, "[C] execute_hebbian_update_on_gpu: Error - Invalid non-positive output dimensions (K=%d, N=%d).\n", K, N); return 0; }
    if (B <= 0 || M <= 0) { fprintf(stderr, "[C] execute_hebbian_update_on_gpu: Error - Invalid non-positive reduction dimensions (B=%d, M=%d).\n", B, M); return 0; }
    if (!hebbian_update_local_reduce_kernel) { fprintf(stderr, "[C] execute_hebbian_update_on_gpu: Error - Hebbian kernel not compiled/available.\n"); return 0; }
    HebbianUpdateLocalReduceCommandData cmd_data = { buffer_a, buffer_c, buffer_w, learning_rate, B, M, N, K };
    if (!submit_kernel_command(gpu_index, COMMAND_HEBBIAN_OUTER_PRODUCT_UPDATE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_threshold_spike_on_gpu(int gpu_index, void* buffer_activations, void* buffer_spikes, float threshold, int num_elements) {
    if (!buffer_activations || !buffer_spikes) { fprintf(stderr, "[C] execute_threshold_spike_on_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (num_elements <= 0) { if (num_elements == 0) return 1; fprintf(stderr, "[C] execute_threshold_spike_on_gpu: Error - Invalid non-positive number of elements (%d).\n", num_elements); return 0; }
    ThresholdSpikeCommandData cmd_data = { buffer_activations, buffer_spikes, threshold, num_elements };
    if (!submit_kernel_command(gpu_index, COMMAND_THRESHOLD_SPIKE, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_dynamic_token_assignment_gpu(int gpu_index, void* activations_bse, void* prototypes_te, void* output_indices_bs, int B, int S, int E, int T) {
    if (!activations_bse || !prototypes_te || !output_indices_bs) { fprintf(stderr, "[C] execute_dynamic_token_assignment_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (B <= 0 || S <= 0) { if ((size_t)B * S == 0) return 1; fprintf(stderr, "[C] execute_dynamic_token_assignment_gpu: Error - Invalid non-positive dimensions (B=%d, S=%d).\n", B, S); return 0; }
    if (E <= 0 || T <= 0) { fprintf(stderr, "[C] execute_dynamic_token_assignment_gpu: Error - Invalid non-positive dimensions (E=%d, T=%d).\n", E, T); return 0; }
    DynamicTokenAssignmentCommandData cmd_data = { activations_bse, prototypes_te, output_indices_bs, B, S, E, T };
    if (!submit_kernel_command(gpu_index, COMMAND_DYNAMIC_TOKEN_ASSIGNMENT, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_pairwise_similarity_gpu(int gpu_index, void* states_nd, void* output_similarity_nn, int N, int D) {
    if (!states_nd || !output_similarity_nn) { fprintf(stderr, "[C] execute_pairwise_similarity_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (N <= 0) { if (N == 0) return 1; fprintf(stderr, "[C] execute_pairwise_similarity_gpu: Error - Invalid non-positive dimension N=%d.\n", N); return 0; }
    if (D <= 0) { fprintf(stderr, "[C] execute_pairwise_similarity_gpu: Error - Invalid non-positive dimension D=%d.\n", D); return 0; }
    PairwiseSimilarityCommandData cmd_data = { states_nd, output_similarity_nn, N, D };
    if (!submit_kernel_command(gpu_index, COMMAND_PAIRWISE_SIMILARITY, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_proto_segmented_sum_gpu(int gpu_index, void* activations_flat, void* indices_flat, void* proto_sums, void* proto_counts, int num_elements_flat, int E, int T) {
    if (!activations_flat || !indices_flat || !proto_sums || !proto_counts) { fprintf(stderr, "[C] execute_proto_segmented_sum_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (!has_atomics_support) { fprintf(stderr, "[C] execute_proto_segmented_sum_gpu: Error - Required atomics support is NOT available on this device. Cannot execute.\n"); return 0; }
    if (num_elements_flat <= 0) { if (num_elements_flat == 0) return 1; fprintf(stderr, "[C] execute_proto_segmented_sum_gpu: Error - Invalid non-positive num_elements_flat (%d).\n", num_elements_flat); return 0;}
    if (E <= 0 || T <= 0) { fprintf(stderr, "[C] execute_proto_segmented_sum_gpu: Error - Invalid non-positive dimensions (E=%d, T=%d).\n", E, T); return 0;}
    ProtoSegmentedSumCommandData cmd_data = { activations_flat, indices_flat, proto_sums, proto_counts, num_elements_flat, E, T };
    if (!submit_kernel_command(gpu_index, COMMAND_PROTO_SEGMENTED_SUM, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_proto_update_step_gpu(int gpu_index, void* prototypes, void* proto_sums, void* proto_counts, float learning_rate, int E, int T) {
    if (!prototypes || !proto_sums || !proto_counts) { fprintf(stderr, "[C] execute_proto_update_step_gpu: Error - NULL buffer handle provided.\n"); return 0; }
    if (T <= 0) { if (T == 0) return 1; fprintf(stderr, "[C] execute_proto_update_step_gpu: Error - Invalid non-positive dimension T (%d).\n", T); return 0;}
    if (E <= 0) { fprintf(stderr, "[C] execute_proto_update_step_gpu: Error - Invalid non-positive dimension E (%d).\n", E); return 0;}
    if (learning_rate < 0.0f || learning_rate > 1.0f) { fprintf(stderr, "[C] execute_proto_update_step_gpu: Warning - Invalid learning_rate (%f). Should be in [0, 1].\n", learning_rate); }
    ProtoUpdateStepCommandData cmd_data = { prototypes, proto_sums, proto_counts, learning_rate, E, T };
    if (!submit_kernel_command(gpu_index, COMMAND_PROTO_UPDATE_STEP, &cmd_data)) { return 0; }
    return 1;
}
DLLEXPORT int execute_shape_loss_with_reward_penalty_gpu(
    int gpu_index,
    void* loss_per_sample_in,
    void* predictions,
    void* targets,
    void* loss_per_sample_out,
    int num_samples,
    int num_classes,
    float penalty_weight,
    float reward_weight,
    float high_confidence_threshold,
    int critical_target_class,
    int critical_predicted_class
) {
    if (!loss_per_sample_in || !predictions || !targets || !loss_per_sample_out) {
        fprintf(stderr, "[C] execute_shape_loss_gpu: Error - NULL buffer handle provided.\n"); return 0;
    }
    if (num_samples <= 0 || num_classes <= 0) {
        if (num_samples == 0) return 1;
        fprintf(stderr, "[C] execute_shape_loss_gpu: Error - Invalid non-positive dimensions (samples=%d, classes=%d).\n", num_samples, num_classes); return 0;
    }
    if (!shape_loss_reward_penalty_kernel) {
         fprintf(stderr, "[C] execute_shape_loss_gpu: Error - Loss shaping kernel not available/compiled.\n"); return 0;
    }
    ShapeLossRewardPenaltyCommandData cmd_data = {
        loss_per_sample_in, predictions, targets, loss_per_sample_out,
        num_samples, num_classes, penalty_weight, reward_weight,
        high_confidence_threshold, critical_target_class, critical_predicted_class
    };
    if (!submit_kernel_command(gpu_index, COMMAND_SHAPE_LOSS_REWARD_PENALTY, &cmd_data)) { return 0; }
    return 1;
}

// Header / Sichtbarkeit
DLLEXPORT int execute_fused_diffusion_on_gpu(
    int gpu_index,
    void* buffer_X,
    void* buffer_W,
    void* buffer_O,
    int B, int N, int D,
    float gamma, float sigma
) {
    if (!buffer_X || !buffer_W || !buffer_O) {
        fprintf(stderr, "[C] execute_fused_diffusion_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (B <= 0 || N <= 0 || D <= 0) {
        if ((size_t)B * N * D == 0) return 1;
        fprintf(stderr, "[C] execute_fused_diffusion_on_gpu: Error - Invalid non-positive dimensions (B=%d, N=%d, D=%d).\n", B, N, D);
        return 0;
    }

    // Wähle den schnellen Kernel, wenn verfügbar, sonst den Standard-Kernel
    cl_kernel kernel = fused_diffusion_kernel_fast ? fused_diffusion_kernel_fast : fused_diffusion_kernel;
    if (!kernel) {
        fprintf(stderr, "[C] execute_fused_diffusion_on_gpu: Error - Fused diffusion kernel not compiled.\n");
        return 0;
    }

    // Erzeuge einen Seed für den Zufallszahlengenerator
    unsigned int seed = (unsigned int)time(NULL) + g_rng_seed++;

    cl_mem x_mem = (cl_mem)buffer_X;
    cl_mem w_mem = (cl_mem)buffer_W;
    cl_mem o_mem = (cl_mem)buffer_O;
    cl_int err = CL_SUCCESS;

    // Setze alle Kernel-Argumente
    err |= clSetKernelArg(kernel, 0, sizeof(cl_mem), &x_mem);
    err |= clSetKernelArg(kernel, 1, sizeof(cl_mem), &w_mem);
    err |= clSetKernelArg(kernel, 2, sizeof(cl_mem), &o_mem);
    err |= clSetKernelArg(kernel, 3, sizeof(cl_int), &B);
    err |= clSetKernelArg(kernel, 4, sizeof(cl_int), &N);
    err |= clSetKernelArg(kernel, 5, sizeof(cl_int), &D);
    err |= clSetKernelArg(kernel, 6, sizeof(cl_float), &gamma);
    err |= clSetKernelArg(kernel, 7, sizeof(cl_float), &sigma);
    err |= clSetKernelArg(kernel, 8, sizeof(cl_uint), &seed); // Das neue Seed-Argument

    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] FusedDiffusion: clSetKernelArg failed: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    size_t total_elements = (size_t)B * N * D;
    size_t gws[1] = { total_elements };

    err = clEnqueueNDRangeKernel(queue, kernel, 1, NULL, gws, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] FusedDiffusion: clEnqueueNDRangeKernel failed: %s (%d)\n", clGetErrorString(err), err);
        return 0;
    }

    // Warten Sie auf die Fertigstellung (da die Python-Seite blockierend ist)
    finish_queue_and_check(gpu_index, "execute_fused_diffusion_on_gpu");

    return 1;
}

DLLEXPORT int execute_izhikevich_step_on_gpu(
    int gpu_index,
    void* v,
    void* u,
    void* i_inj,
    void* spikes_out,
    void* p_a,
    void* p_b,
    void* p_c,
    void* p_d,
    float dt,
    float threshold,
    int num_neurons
) {
    if (!v || !u || !i_inj || !spikes_out || !p_a || !p_b || !p_c || !p_d) {
        fprintf(stderr, "[C] execute_izhikevich_step_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (num_neurons <= 0) {
        if (num_neurons == 0) { return 1; }
        fprintf(stderr, "[C] execute_izhikevich_step_on_gpu: Error - Invalid neuron count (%d).\n", num_neurons);
        return 0;
    }
    if (dt <= 0.0f) {
        fprintf(stderr, "[C] execute_izhikevich_step_on_gpu: Error - dt must be positive (%.6f).\n", dt);
        return 0;
    }
    IzhikevichCommandData cmd = { v, u, i_inj, spikes_out, p_a, p_b, p_c, p_d, dt, threshold, num_neurons };
    if (!submit_kernel_command(gpu_index, COMMAND_IZHIKEVICH_STEP, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_stdp_update_on_gpu(
    int gpu_index,
    void* weights,
    void* pre_traces,
    void* post_traces,
    void* pre_spike_events,
    void* post_spike_events,
    float lr_ltp,
    float lr_ltd,
    int pre_n,
    int post_n
) {
    if (!weights || !pre_traces || !post_traces || !pre_spike_events || !post_spike_events) {
        fprintf(stderr, "[C] execute_stdp_update_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (pre_n <= 0 || post_n <= 0) {
        if (pre_n == 0 || post_n == 0) { return 1; }
        fprintf(stderr, "[C] execute_stdp_update_on_gpu: Error - Invalid dimensions (pre=%d, post=%d).\n", pre_n, post_n);
        return 0;
    }
    if (lr_ltp < 0.0f || lr_ltd < 0.0f) {
        fprintf(stderr, "[C] execute_stdp_update_on_gpu: Warning - Negative learning rates (ltp=%.6f, ltd=%.6f).\n", lr_ltp, lr_ltd);
    }
    STDPUpdateCommandData cmd = { weights, pre_traces, post_traces, pre_spike_events, post_spike_events, lr_ltp, lr_ltd, pre_n, post_n };
    if (!submit_kernel_command(gpu_index, COMMAND_STDP_UPDATE, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_stdp_trace_update_on_gpu(
    int gpu_index,
    void* pre_traces,
    void* post_traces,
    void* pre_spike_events,
    void* post_spike_events,
    float decay_pre,
    float decay_post,
    float increment_pre,
    float increment_post,
    int pre_n,
    int post_n
) {
    if (!pre_traces || !post_traces || !pre_spike_events || !post_spike_events) {
        fprintf(stderr, "[C] execute_stdp_trace_update_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (pre_n < 0 || post_n < 0) {
        fprintf(stderr, "[C] execute_stdp_trace_update_on_gpu: Error - Negative dimensions (pre=%d, post=%d).\n", pre_n, post_n);
        return 0;
    }
    int max_dim = (pre_n > post_n) ? pre_n : post_n;
    if (max_dim == 0) { return 1; }
    STDPTraceCommandData cmd = { pre_traces, post_traces, pre_spike_events, post_spike_events, decay_pre, decay_post, increment_pre, increment_post, pre_n, post_n };
    if (!submit_kernel_command(gpu_index, COMMAND_STDP_TRACE_UPDATE, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_lbm_collide_and_stream_on_gpu(
    int gpu_index,
    void* f_in,
    void* f_out,
    void* rho,
    void* ux,
    void* uy,
    float omega,
    int width,
    int height
) {
    if (!f_in || !f_out || !rho || !ux || !uy) {
        fprintf(stderr, "[C] execute_lbm_collide_and_stream_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (width <= 0 || height <= 0) {
        if (width == 0 || height == 0) { return 1; }
        fprintf(stderr, "[C] execute_lbm_collide_and_stream_on_gpu: Error - Invalid grid size (w=%d, h=%d).\n", width, height);
        return 0;
    }
    if (omega <= 0.0f) {
        fprintf(stderr, "[C] execute_lbm_collide_and_stream_on_gpu: Error - omega must be positive (%.6f).\n", omega);
        return 0;
    }
    LBMCollideStreamCommandData cmd = { f_in, f_out, rho, ux, uy, omega, width, height };
    if (!submit_kernel_command(gpu_index, COMMAND_LBM_COLLIDE_STREAM, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_nbody_calculate_forces_on_gpu(
    int gpu_index,
    void* positions,
    void* forces,
    float gravitational_const,
    float softening_factor,
    int num_bodies
) {
    if (!positions || !forces) {
        fprintf(stderr, "[C] execute_nbody_calculate_forces_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (num_bodies <= 0) {
        if (num_bodies == 0) { return 1; }
        fprintf(stderr, "[C] execute_nbody_calculate_forces_on_gpu: Error - Invalid body count (%d).\n", num_bodies);
        return 0;
    }
    NBodyForcesCommandData cmd = { positions, forces, gravitational_const, softening_factor, num_bodies };
    if (!submit_kernel_command(gpu_index, COMMAND_NBODY_FORCES, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_nbody_integrate_on_gpu(
    int gpu_index,
    void* positions,
    void* velocities,
    void* forces,
    float dt,
    int num_bodies
) {
    if (!positions || !velocities || !forces) {
        fprintf(stderr, "[C] execute_nbody_integrate_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (num_bodies <= 0) {
        if (num_bodies == 0) { return 1; }
        fprintf(stderr, "[C] execute_nbody_integrate_on_gpu: Error - Invalid body count (%d).\n", num_bodies);
        return 0;
    }
    NBodyIntegrateCommandData cmd = { positions, velocities, forces, dt, num_bodies };
    if (!submit_kernel_command(gpu_index, COMMAND_NBODY_INTEGRATE, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_ising_metropolis_step_on_gpu(
    int gpu_index,
    void* spin_grid,
    void* random_numbers,
    float J,
    float beta,
    int width,
    int height,
    int color
) {
    if (!spin_grid || !random_numbers) {
        fprintf(stderr, "[C] execute_ising_metropolis_step_on_gpu: Error - NULL buffer handle provided.\n");
        return 0;
    }
    if (width <= 0 || height <= 0) {
        if (width == 0 || height == 0) { return 1; }
        fprintf(stderr, "[C] execute_ising_metropolis_step_on_gpu: Error - Invalid grid size (w=%d, h=%d).\n", width, height);
        return 0;
    }
    if ((color & ~1) != 0) {
        fprintf(stderr, "[C] execute_ising_metropolis_step_on_gpu: Error - color must be 0 or 1 (got %d).\n", color);
        return 0;
    }
    IsingMetropolisCommandData cmd = { spin_grid, random_numbers, J, beta, width, height, color };
    if (!submit_kernel_command(gpu_index, COMMAND_ISING_METROPOLIS, &cmd)) { return 0; }
    return 1;
}

DLLEXPORT int execute_shape_loss_with_reward_penalty_list_gpu(
    int gpu_index,
    void* loss_per_sample_in,
    void* predictions,
    void* targets,
    void* loss_per_sample_out,
    void* critical_pairs,
    int num_samples,
    int num_classes,
    int num_critical_pairs,
    float penalty_weight,
    float reward_weight,
    float high_confidence_threshold
) {
    if (!loss_per_sample_in || !predictions || !targets || !loss_per_sample_out) {
        fprintf(stderr, "[C] execute_shape_loss_list_gpu: Error - NULL required buffer handle provided.\n"); return 0;
    }
    if (num_critical_pairs > 0 && !critical_pairs) {
         fprintf(stderr, "[C] execute_shape_loss_list_gpu: Error - Critical pairs buffer is NULL but count is %d.\n", num_critical_pairs); return 0;
    }
    if (num_samples <= 0 || num_classes <= 0) {
        if (num_samples == 0) return 1;
        fprintf(stderr, "[C] execute_shape_loss_list_gpu: Error - Invalid non-positive dimensions (samples=%d, classes=%d).\n", num_samples, num_classes); return 0;
    }
    if (!shape_loss_reward_penalty_list_kernel) {
         fprintf(stderr, "[C] execute_shape_loss_list_gpu: Error - Loss shaping list kernel not available/compiled.\n"); return 0;
    }
    ShapeLossRewardPenaltyListCommandData cmd_data = {
        loss_per_sample_in, predictions, targets, loss_per_sample_out,
        critical_pairs,
        num_samples, num_classes, num_critical_pairs,
        penalty_weight, reward_weight, high_confidence_threshold
    };
    if (!submit_kernel_command(gpu_index, COMMAND_SHAPE_LOSS_REWARD_PENALTY_LIST, &cmd_data)) {
        return 0;
    }
    return 1;
}

DLLEXPORT int sqse_load_kernels(const char* kernel_path) {
    (void)kernel_path; // Kernel source embedded; path retained for API compatibility.
    return ensure_sqse_kernels_ready() ? 0 : -1;
}

static int sqse_validate_common(const float* ptr, int n, const char* label) {
    if (!ptr) {
        fprintf(stderr, "[C] SQSE: Error - NULL pointer for %s.\n", label);
        return -1;
    }
    if (n < 0) {
        fprintf(stderr, "[C] SQSE: Error - Negative element count (%d).\n", n);
        return -1;
    }
    return 0;
}

DLLEXPORT int execute_sqse_encrypt_float(const float* data_in,
                                         const float* key,
                                         int n,
                                         float chaos_K,
                                         int steps,
                                         float* out_theta,
                                         float* out_p_masked) {
    if (sqse_validate_common(data_in, n, "data_in") < 0 ||
        sqse_validate_common(key, n, "key") < 0 ||
        sqse_validate_common(out_theta, n, "out_theta") < 0 ||
        sqse_validate_common(out_p_masked, n, "out_p_masked") < 0) {
        return -1;
    }
    if (n == 0) {
        return 0;
    }
    if (steps < 0) {
        fprintf(stderr, "[C] SQSE: Error - Negative iteration steps (%d).\n", steps);
        return -1;
    }
    if (!context || !queue) {
        fprintf(stderr, "[C] SQSE: Error - OpenCL context/queue not initialized. Call initialize_gpu first.\n");
        return -2;
    }
    if (!ensure_sqse_kernels_ready()) {
        return -3;
    }

    cl_int err = CL_SUCCESS;
    size_t bytes = (size_t)n * sizeof(float);
    cl_mem buf_data = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, (void*)data_in, &err);
    if (err != CL_SUCCESS || !buf_data) {
        fprintf(stderr, "[C] SQSE Encrypt: clCreateBuffer data failed: %s (%d)\n", clGetErrorString(err), err);
        return -4;
    }
    cl_mem buf_key = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, (void*)key, &err);
    if (err != CL_SUCCESS || !buf_key) {
        fprintf(stderr, "[C] SQSE Encrypt: clCreateBuffer key failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        return -4;
    }
    cl_mem buf_theta = clCreateBuffer(context, CL_MEM_WRITE_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !buf_theta) {
        fprintf(stderr, "[C] SQSE Encrypt: clCreateBuffer out_theta failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        clReleaseMemObject(buf_key);
        return -4;
    }
    cl_mem buf_p_masked = clCreateBuffer(context, CL_MEM_WRITE_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !buf_p_masked) {
        fprintf(stderr, "[C] SQSE Encrypt: clCreateBuffer out_p_masked failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_theta);
        return -4;
    }

    int arg_idx = 0;
    err  = clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_data);
    err |= clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_key);
    err |= clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(float), &chaos_K);
    err |= clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(int), &steps);
    err |= clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_theta);
    err |= clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_p_masked);
    err |= clSetKernelArg(sqse_encrypt_kernel, arg_idx++, sizeof(int), &n);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Encrypt: clSetKernelArg failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        return -5;
    }

    size_t global = (size_t)n;
    err = clEnqueueNDRangeKernel(queue, sqse_encrypt_kernel, 1, NULL, &global, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Encrypt: clEnqueueNDRangeKernel failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        return -6;
    }

    err = clEnqueueReadBuffer(queue, buf_theta, CL_TRUE, 0, bytes, out_theta, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Encrypt: Read out_theta failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        return -7;
    }
    err = clEnqueueReadBuffer(queue, buf_p_masked, CL_TRUE, 0, bytes, out_p_masked, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Encrypt: Read out_p_masked failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_data);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        return -7;
    }

    clFinish(queue);

    clReleaseMemObject(buf_data);
    clReleaseMemObject(buf_key);
    clReleaseMemObject(buf_theta);
    clReleaseMemObject(buf_p_masked);
    return 0;
}

DLLEXPORT int execute_sqse_decrypt_float(const float* in_theta,
                                         const float* in_p_masked,
                                         const float* key,
                                         int n,
                                         float chaos_K,
                                         int steps,
                                         float* data_out) {
    if (sqse_validate_common(in_theta, n, "in_theta") < 0 ||
        sqse_validate_common(in_p_masked, n, "in_p_masked") < 0 ||
        sqse_validate_common(key, n, "key") < 0 ||
        sqse_validate_common(data_out, n, "data_out") < 0) {
        return -1;
    }
    if (n == 0) {
        return 0;
    }
    if (steps < 0) {
        fprintf(stderr, "[C] SQSE: Error - Negative iteration steps (%d).\n", steps);
        return -1;
    }
    if (!context || !queue) {
        fprintf(stderr, "[C] SQSE: Error - OpenCL context/queue not initialized. Call initialize_gpu first.\n");
        return -2;
    }
    if (!ensure_sqse_kernels_ready()) {
        return -3;
    }

    cl_int err = CL_SUCCESS;
    size_t bytes = (size_t)n * sizeof(float);
    cl_mem buf_theta = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, (void*)in_theta, &err);
    if (err != CL_SUCCESS || !buf_theta) {
        fprintf(stderr, "[C] SQSE Decrypt: clCreateBuffer in_theta failed: %s (%d)\n", clGetErrorString(err), err);
        return -4;
    }
    cl_mem buf_p_masked = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, (void*)in_p_masked, &err);
    if (err != CL_SUCCESS || !buf_p_masked) {
        fprintf(stderr, "[C] SQSE Decrypt: clCreateBuffer in_p_masked failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_theta);
        return -4;
    }
    cl_mem buf_key = clCreateBuffer(context, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, bytes, (void*)key, &err);
    if (err != CL_SUCCESS || !buf_key) {
        fprintf(stderr, "[C] SQSE Decrypt: clCreateBuffer key failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        return -4;
    }
    cl_mem buf_out = clCreateBuffer(context, CL_MEM_WRITE_ONLY, bytes, NULL, &err);
    if (err != CL_SUCCESS || !buf_out) {
        fprintf(stderr, "[C] SQSE Decrypt: clCreateBuffer data_out failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        clReleaseMemObject(buf_key);
        return -4;
    }

    int arg_idx = 0;
    err  = clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_theta);
    err |= clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_p_masked);
    err |= clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_key);
    err |= clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(float), &chaos_K);
    err |= clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(int), &steps);
    err |= clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(cl_mem), &buf_out);
    err |= clSetKernelArg(sqse_decrypt_kernel, arg_idx++, sizeof(int), &n);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Decrypt: clSetKernelArg failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_out);
        return -5;
    }

    size_t global = (size_t)n;
    err = clEnqueueNDRangeKernel(queue, sqse_decrypt_kernel, 1, NULL, &global, NULL, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Decrypt: clEnqueueNDRangeKernel failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_out);
        return -6;
    }

    err = clEnqueueReadBuffer(queue, buf_out, CL_TRUE, 0, bytes, data_out, 0, NULL, NULL);
    if (err != CL_SUCCESS) {
        fprintf(stderr, "[C] SQSE Decrypt: Read data_out failed: %s (%d)\n", clGetErrorString(err), err);
        clReleaseMemObject(buf_theta);
        clReleaseMemObject(buf_p_masked);
        clReleaseMemObject(buf_key);
        clReleaseMemObject(buf_out);
        return -7;
    }

    clFinish(queue);

    clReleaseMemObject(buf_theta);
    clReleaseMemObject(buf_p_masked);
    clReleaseMemObject(buf_key);
    clReleaseMemObject(buf_out);
    return 0;
}

DLLEXPORT void set_noise_level(int gpu_index, float value) {
    (void)gpu_index;
    set_noise_factor(value);
}

DLLEXPORT float get_noise_level(int gpu_index) {
    (void)gpu_index;
    return get_noise_factor();
}

DLLEXPORT void register_kernel_measurement_buffers(float* error_ptr, float* variance_ptr) {
    g_measurement_error_target = error_ptr;
    g_measurement_variance_target = variance_ptr;
}

DLLEXPORT void reset_kernel_measurement_buffers(void) {
    g_measurement_error_target = NULL;
    g_measurement_variance_target = NULL;
}

DLLEXPORT int get_last_kernel_metrics(int gpu_index, KernelMetricsSample* out_metrics) {
    (void)gpu_index;
    if (!out_metrics) {
        return 0;
    }
    *out_metrics = g_last_metrics;
    return 1;
}


// --- Simulation Layer (Dummy implementations) ---
DLLEXPORT unsigned long long simulated_kernel_allocate(int gpu_index, size_t size) {
    if (size == 0) return 0;
    void* ptr = malloc(size);
    if (!ptr) { fprintf(stderr, "[C SIM] simulated_kernel_allocate: malloc failed for size %zu.\n", size); return 0; }
    return (unsigned long long)(uintptr_t)ptr;
}
DLLEXPORT void simulated_kernel_free(int gpu_index, unsigned long long address, size_t size) {
    if (address == 0) return;
    free((void*)(uintptr_t)address);
}
DLLEXPORT void simulated_kernel_write(int gpu_index, unsigned long long address, size_t size, const void *source) {
    if (address == 0 || size == 0 || source == NULL) return;
    memcpy((void*)(uintptr_t)address, source, size);
}
DLLEXPORT unsigned int simulated_get_compute_unit_count(int gpu_index) {
    return 4;
}

// #ifdef _DEBUG
// Beispiel (Mini-Smoke-Test): num_qubits = 4, depth = 2 => U_gate_count ≈ (3*4 + (4-1)) * 2 = 30.
// QuantumGate U_seq[30]; // mit abwechselnden RZ/RY/RX-Rotationen (theta = 0.3f) und CNOT-Kaskaden.
// QuantumGate W = {"RX", 1, 0, 1, 0, {0.3f, 0.0f, 0.0f, 0.0f}, {{{0}}}}; // Lokale Störung auf Qubit 1.
// QuantumGate V = {"RZ", 1, 0, 2, 0, {0.3f, 0.0f, 0.0f, 0.0f}, {{{0}}}}; // Beobachter auf Qubit 2 (optional).
// float L = 0.0f, otoc_re = 0.0f, otoc_im = 0.0f;
// execute_quantum_echoes_otoc_gpu(0, 4, U_seq, 30, &W, &V, 1, &L, &otoc_re, &otoc_im);
// Erwartung: 0.0f < L < 1.0f und komplexe OTOC(2)-Amplitude aus amp[0].
// #endif

// --- End of File ---