# chipgraph example: tinysoc
# SPDX-License-Identifier: Apache-2.0
#
# Meant to be invoked with this directory as the working directory (that is how
# chipgraph's `cmd` tool adapter runs a project's own commands: repo_root == this
# directory when `.chipgraph.yml`'s adapters point at `make lint BLOCK={block}`).

BLOCKS := timer gpio top

.PHONY: lint lint-all clean

# lint: run Verilator in lint-only mode over one block's filelist.
#   make lint BLOCK=timer
lint:
	@if [ -z "$(BLOCK)" ]; then \
		echo "usage: make lint BLOCK=<timer|gpio|top>" >&2; \
		exit 2; \
	fi
	verilator --lint-only -Wall -f filelists/$(BLOCK).f --top-module tiny_$(BLOCK)

lint-all:
	@for b in $(BLOCKS); do \
		$(MAKE) lint BLOCK=$$b || exit 1; \
	done

clean:
	rm -rf build obj_dir
