# chipgraph example: tinysoc
# SPDX-License-Identifier: Apache-2.0
#
# Meant to be invoked with this directory as the working directory (that is how
# chipgraph's `cmd` tool adapter runs a project's own commands: repo_root == this
# directory when `.chipgraph.yml`'s adapters point at `make lint BLOCK={block}`).

BLOCKS := timer gpio top

# The wrappers of a block, each linted as its own top with filelists/<wrapper>.f: the
# generated wrapper plus the vendor IP's filelist (vendor/<ip>/<ip>.f).
WRAPPERS_top := tiny_fifo_wrap
WRAPPER_LINT := verilator --lint-only -Wall

.PHONY: lint lint-all clean

# lint: run Verilator in lint-only mode over one block's filelist, then each of its wrappers.
#   make lint BLOCK=timer
lint:
	@if [ -z "$(BLOCK)" ]; then \
		echo "usage: make lint BLOCK=<timer|gpio|top>" >&2; \
		exit 2; \
	fi
	verilator --lint-only -Wall -f filelists/$(BLOCK).f --top-module tiny_$(BLOCK)
	@for w in $(WRAPPERS_$(BLOCK)); do \
		echo "$(WRAPPER_LINT) -f filelists/$$w.f --top-module $$w"; \
		$(WRAPPER_LINT) -f filelists/$$w.f --top-module $$w || exit 1; \
	done

lint-all:
	@for b in $(BLOCKS); do \
		$(MAKE) lint BLOCK=$$b || exit 1; \
	done

clean:
	rm -rf build obj_dir
