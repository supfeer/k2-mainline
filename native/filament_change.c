/* Reconstructed from the installed snapshot's ARM filament_change.o.
 * SHA256: b866026249d844cc86a7fcef453fb27a6a2b7ddd097b7fc88eb3064474df7e91
 * Preserve the original float/double boundaries; do not use fast-math/FMA.
 */
#include <math.h>
#include <stdio.h>
#include "filament_change.h"

static void
rgb_to_hsv(float r, float g, float b, float *h, float *s, float *v)
{
    /* Object offsets 0x000-0x10c. Negative red-sector hue is intentional. */
    float maximum = fmax(fmax(r, g), b);
    float minimum = fmin(fmin(r, g), b);
    float delta = maximum - minimum;
    if ((double)fabsf(delta) < 0.001)
        *h = 0.0f;
    else if (r == maximum)
        *h = fmod((double)((g - b) / delta), 6.0) * 60.0;
    else if (g == maximum)
        *h = ((b - r) / delta + 2.0f) * 60.0f;
    else
        *h = ((r - g) / delta + 4.0f) * 60.0f;
    *s = (double)fabsf(maximum) < 0.001 ? 0.0f : delta / maximum;
    *v = maximum;
}

static float
hsv_distance(float h1, float s1, float v1, float h2, float s2, float v2)
{
    /* 0x1d8-0x29c: radians round to float before double sin/cos. */
    float angle1 = (double)(h1 / 180.0f) * 0x1.921fb54442d18p+1;
    float angle2 = (double)(h2 / 180.0f) * 0x1.921fb54442d18p+1;
    float dx = cos((double)angle1) * s1 * v1
             - cos((double)angle2) * s2 * v2;
    float dy = sin((double)angle1) * s1 * v1
             - sin((double)angle2) * s2 * v2;
    float squared = dx * dx + dy * dy;
    return fmin(0x1.333334p+0, (double)sqrtf(squared));
}

static float
luminance(float r, float g, float b)
{
    /* 0x29c-0x2e4: double coefficients, one final float rounding. */
    return (double)g * 0.59 + (double)r * 0.3 + (double)b * 0.11;
}

static int
calculate_flushing_volume(const rgb_t source, const rgb_t target)
{
    float r1 = source.r / 255.0f;
    float g1 = source.g / 255.0f;
    float b1 = source.b / 255.0f;
    float r2 = target.r / 255.0f;
    float g2 = target.g / 255.0f;
    float b2 = target.b / 255.0f;
    float h1, s1, v1, h2, s2, v2;
    rgb_to_hsv(r1, g1, b1, &h1, &s1, &v1);
    rgb_to_hsv(r2, g2, b2, &h2, &s2, &v2);
    float distance = hsv_distance(h1, s1, v1, h2, s2, v2);
    float from_luminance = luminance(r1, g1, b1);
    float to_luminance = luminance(r2, g2, b2);
    float luminance_volume;
    if (from_luminance > to_luminance) {
        /* 0x2f0-0x32c. Weighted value is rounded before fmin. */
        luminance_volume = (from_luminance - to_luminance) * 80.0f;
        float intermediate_value = (double)v1 * 0.33 + (double)v2 * 0.67;
        distance = fmin((double)intermediate_value, (double)distance);
    } else {
        /* 0x3ac-0x3c8. Exponent is float 0.7 promoted to double. */
        float difference = to_luminance - from_luminance;
        luminance_volume = pow((double)difference, 0x1.666666p-1) * 560.0;
    }
    /* 0x330-0x36c. Do not replace the stored cosine by exactly -0.5. */
    float hue_volume = distance * 230.0f;
    float squared = luminance_volume * luminance_volume
                  + hue_volume * hue_volume;
    float product = (hue_volume + hue_volume) * luminance_volume;
    double triangle = (double)squared
                    - (double)product * -0x1.000001b1908afp-1;
    float volume = sqrt(triangle);
    volume = fmax((double)volume, 60.0);
    /* 0x370-0x39c: configured minimum addition is zero; truncate, cap 800. */
    return (int)fmin((double)(int)volume, 800.0);
}

/* Keep the exported diagnostic ABI without preempting its calculation. */
__attribute__((visibility("default"), externally_visible, used))
const float m_max_flush_volume = 800.0f;

int
get_flushing_volume(const rgb_t source, const rgb_t target)
{
    return calculate_flushing_volume(source, target);
}

__attribute__((visibility("default"), externally_visible))
int main(void)
{
    const rgb_t source = {0, 0, 0};
    const rgb_t target = {225, 55, 180};
    printf("volume = %d\n", calculate_flushing_volume(source, target));
    return 0;
}
