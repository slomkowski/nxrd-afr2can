# Point OPENPORT_DIR at a built openport-j2534 checkout (make all there first).
OPENPORT_DIR ?= $(HOME)/projects/openport-j2534

CFLAGS ?= -O2 -g -Wall -Wextra -Wpedantic
CFLAGS += -std=c11 -D_POSIX_C_SOURCE=200809L -I$(OPENPORT_DIR)/include

afr_logger: afr_logger.c
	$(CC) $(CFLAGS) $< -L$(OPENPORT_DIR) -Wl,-rpath,$(OPENPORT_DIR) -lj2534 -o $@

clean:
	rm -f afr_logger
.PHONY: clean
