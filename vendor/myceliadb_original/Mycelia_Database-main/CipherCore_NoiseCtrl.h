#ifndef CIPHERCORE_NOISECTRL_H
#define CIPHERCORE_NOISECTRL_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

extern float g_noise_factor;

void update_noise(float variance);
void set_noise_factor(float value);
float get_noise_factor(void);
void reset_noise_factor(void);
void noisectrl_measure(float variance, float* error_out, float* variance_out);

#ifdef __cplusplus
}
#endif

#endif /* CIPHERCORE_NOISECTRL_H */
