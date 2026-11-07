/*
 * afr_logger — log wideband lambda/AFR from a rusEFI/mck1117 wideband
 * controller over CAN, through the openport-j2534 library (Tactrix OpenPort 2.0).
 *
 * Frames, from wideband-can.dbc (all little-endian, 11-bit ids):
 *   0x190+2N  standard: u8 version, u8 valid, u16 lambda*10000, u16 temp degC
 *   0x191+2N  diag:     u16 esr ohm, u16 nernst mV, u8 pump%, u8 status, u8 heater%
 * N is the controller index.
 *
 * Output is CSV on stdout, one line per standard frame, with the latest diag
 * values for the same controller appended. Diagnostics on stderr.
 */
#include "j2534/j2534.h"

#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define ID_STANDARD  0x190u
#define ID_DIAG      0x191u
#define MAX_CTRL     8            /* ids 0x190..0x19F */

static volatile sig_atomic_t g_stop;
static void on_sigint(int s) { (void)s; g_stop = 1; }

typedef struct {
    int    have;
    double esr, nernst_v, pump_pct, heater_pct;
    unsigned status;
} diag_t;

static unsigned u16le(const unsigned char *p) { return (unsigned)(p[0] | (p[1] << 8)); }

static int die(const char *what, long rc)
{
    char err[80] = "";
    PassThruGetLastError(err);
    fprintf(stderr, "%s failed: %ld %s\n", what, rc, err);
    return 1;
}

static void usage(const char *a0)
{
    fprintf(stderr,
        "usage: %s [-b baud] [-s stoich_afr] [-n seconds]\n"
        "  -b  CAN bit rate           (default 500000)\n"
        "  -s  stoichiometric AFR     (default 14.7 gasoline; E85 9.76)\n"
        "  -n  stop after N seconds   (default: until Ctrl-C)\n", a0);
}

int main(int argc, char **argv)
{
    unsigned long baud = 500000;
    double stoich = 14.7, limit_s = 0;
    J_U32 dev = 0, ch = 0, fid = 0;
    PASSTHRU_MSG mask, pat;
    diag_t diag[MAX_CTRL];
    struct timespec t0;
    long rc;
    int i;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-b") && i + 1 < argc)      baud = strtoul(argv[++i], NULL, 10);
        else if (!strcmp(argv[i], "-s") && i + 1 < argc) stoich = atof(argv[++i]);
        else if (!strcmp(argv[i], "-n") && i + 1 < argc) limit_s = atof(argv[++i]);
        else { usage(argv[0]); return 2; }
    }
    if (baud == 0 || stoich <= 0) { usage(argv[0]); return 2; }

    memset(diag, 0, sizeof diag);
    signal(SIGINT, on_sigint);

    rc = PassThruOpen(NULL, &dev);
    if (rc) return die("PassThruOpen", rc);
    rc = PassThruConnect(dev, CAN, 0, (J_U32)baud, &ch);
    if (rc) { die("PassThruConnect", rc); PassThruClose(dev); return 1; }

    /* Pass 0x190..0x19F: both frames of up to eight controllers. */
    memset(&mask, 0, sizeof mask);
    memset(&pat, 0, sizeof pat);
    mask.ProtocolID = pat.ProtocolID = CAN;
    mask.DataSize = pat.DataSize = 4;
    mask.Data[2] = 0x07; mask.Data[3] = 0xF0;
    pat.Data[2]  = (unsigned char)(ID_STANDARD >> 8);
    pat.Data[3]  = (unsigned char)(ID_STANDARD & 0xFF);
    rc = PassThruStartMsgFilter(ch, PASS_FILTER, &mask, &pat, NULL, &fid);
    if (rc) { die("PassThruStartMsgFilter", rc); PassThruClose(dev); return 1; }

    printf("time_s,controller,valid,lambda,afr,temp_c,esr_ohm,nernst_v,pump_pct,heater_pct,status\n");
    clock_gettime(CLOCK_MONOTONIC, &t0);

    while (!g_stop) {
        PASSTHRU_MSG in[32];
        J_U32 n = 32, k;
        struct timespec now;
        double t;

        rc = PassThruReadMsgs(ch, in, &n, 200);
        if (rc && rc != ERR_TIMEOUT && rc != ERR_BUFFER_EMPTY && rc != ERR_BUFFER_OVERFLOW) {
            die("PassThruReadMsgs", rc);
            break;
        }
        if (rc == ERR_BUFFER_OVERFLOW) fprintf(stderr, "warning: receive queue overflowed\n");

        clock_gettime(CLOCK_MONOTONIC, &now);
        t = (double)(now.tv_sec - t0.tv_sec) + (double)(now.tv_nsec - t0.tv_nsec) / 1e9;
        if (limit_s > 0 && t >= limit_s) g_stop = 1;

        for (k = 0; k < n; k++) {
            const PASSTHRU_MSG *m = &in[k];
            const unsigned char *d = m->Data + 4;
            unsigned id, ctrl;

            if (m->RxStatus & (TX_MSG_TYPE | START_OF_MESSAGE)) continue;
            if (m->DataSize < 4 + 8) continue;
            id = ((unsigned)m->Data[2] << 8) | m->Data[3];
            if (id < ID_STANDARD || id >= ID_STANDARD + 2 * MAX_CTRL) continue;
            ctrl = (id - ID_STANDARD) / 2;

            if ((id - ID_STANDARD) % 2) {
                diag_t *g = &diag[ctrl];
                g->have       = 1;
                g->esr        = u16le(d);
                g->nernst_v   = u16le(d + 2) * 0.001;
                g->pump_pct   = d[4] * 0.392157;
                g->status     = d[5];
                g->heater_pct = d[6] * 0.392157;
            } else {
                const diag_t *g = &diag[ctrl];
                int valid = d[1] != 0;
                double lambda = u16le(d + 2) * 0.0001;

                printf("%.3f,%u,%d,%.4f,%.2f,%u,", t, ctrl, valid, lambda, lambda * stoich, u16le(d + 4));
                if (g->have)
                    printf("%.0f,%.3f,%.1f,%.1f,0x%02X\n", g->esr, g->nernst_v, g->pump_pct, g->heater_pct, g->status);
                else
                    printf(",,,,\n");
            }
        }
        fflush(stdout);
    }

    PassThruStopMsgFilter(ch, fid);
    PassThruDisconnect(ch);
    PassThruClose(dev);
    return 0;
}
