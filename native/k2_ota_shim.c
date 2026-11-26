/*
 * LD_PRELOAD shim for K2 Plus app-server (libcrealitycloud.so).
 *
 * Redirects only Creality Cloud firmware-check URLs
 * ("/api/cxy/v2/firmware/list") to the local k2-ota-proxy daemon on loopback
 * while forwarding every other libcurl option and URL untouched.
 *
 * Designed for 32-bit ARM EABI (armv7l, GLIBC <= 2.29) where curl_off_t is
 * 64-bit (CURLOPT_OFF_T range 30000..39999) and all other options fit in a
 * single 32-bit word (long / pointer / function pointer).
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define K2_CURLOPT_URL 10002
#define K2_CURLOPTTYPE_OFF_T_MIN 30000
#define K2_CURLOPTTYPE_OFF_T_MAX 39999

#define K2_FIRMWARE_LIST_PATH "/api/cxy/v2/firmware/list"
#define K2_DEFAULT_PROXY_URL "http://127.0.0.1:7129/api/cxy/v2/firmware/list"

typedef int (*k2_curl_setopt_ptr_fn)(void *, int, void *);
typedef int (*k2_curl_setopt_i64_fn)(void *, int, int64_t);

static void *g_real_curl_easy_setopt = NULL;

static void *k2_resolve_real_setopt(void) {
    void *fn = __atomic_load_n(&g_real_curl_easy_setopt, __ATOMIC_ACQUIRE);
    if (fn != NULL) {
        return fn;
    }
    fn = dlsym(RTLD_NEXT, "curl_easy_setopt");
    if (fn != NULL) {
        __atomic_store_n(&g_real_curl_easy_setopt, fn, __ATOMIC_RELEASE);
    }
    return fn;
}

static const char *k2_rewrite_url(const char *url) {
    if (url == NULL) {
        return NULL;
    }
    /* Never rewrite URLs already targeting local loopback proxy. */
    if (strncmp(url, "http://127.0.0.1:", 17) == 0 ||
        strncmp(url, "http://localhost:", 17) == 0) {
        return url;
    }
    if (strstr(url, K2_FIRMWARE_LIST_PATH) == NULL) {
        return url;
    }
    const char *override = getenv("K2_OTA_PROXY_URL");
    if (override != NULL && override[0] != '\0') {
        return override;
    }
    return K2_DEFAULT_PROXY_URL;
}

int curl_easy_setopt(void *curl, int option, ...) {
    void *real_fn = k2_resolve_real_setopt();
    if (real_fn == NULL) {
        /* CURLE_FAILED_INIT = 2 */
        return 2;
    }

    va_list ap;
    va_start(ap, option);

    if (option >= K2_CURLOPTTYPE_OFF_T_MIN && option <= K2_CURLOPTTYPE_OFF_T_MAX) {
        int64_t val64 = va_arg(ap, int64_t);
        va_end(ap);
        return ((k2_curl_setopt_i64_fn)real_fn)(curl, option, val64);
    }

    void *arg = va_arg(ap, void *);
    va_end(ap);

    if (option == K2_CURLOPT_URL && arg != NULL) {
        arg = (void *)k2_rewrite_url((const char *)arg);
    }

    return ((k2_curl_setopt_ptr_fn)real_fn)(curl, option, arg);
}
