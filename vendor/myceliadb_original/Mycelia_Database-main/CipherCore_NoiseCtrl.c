#include "CipherCore_NoiseCtrl.h"

#include <math.h>

#ifndef THRESH_HIGH
#define THRESH_HIGH 1.5f
#endif

#ifndef THRESH_LOW
#define THRESH_LOW 0.5f
#endif

float g_noise_factor = 1.0f;

void update_noise(float variance) {
    if (variance > THRESH_HIGH) {
        g_noise_factor *= 0.9f;
    } else if (variance < THRESH_LOW) {
        g_noise_factor *= 1.1f;
    }
    if (g_noise_factor < 0.1f) {
        g_noise_factor = 0.1f;
    } else if (g_noise_factor > 2.0f) {
        g_noise_factor = 2.0f;
    }
}

void set_noise_factor(float value) {
    if (value < 0.1f) {
        value = 0.1f;
    } else if (value > 2.0f) {
        value = 2.0f;
    }
    g_noise_factor = value;
}

float get_noise_factor(void) {
    return g_noise_factor;
}

void reset_noise_factor(void) {
    g_noise_factor = 1.0f;
}

static float compute_error_from_variance(float variance) {
    float deviation = variance - 1.0f;
    return fabsf(deviation) * 0.5f;
}

void noisectrl_measure(float variance, float* error_out, float* variance_out) {
    update_noise(variance);
    if (variance_out) {
        *variance_out = variance;
    }
    if (error_out) {
        *error_out = compute_error_from_variance(variance);
    }
}
