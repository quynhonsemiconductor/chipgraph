# SPDX-License-Identifier: Apache-2.0
# A cocotb test module that fails to import, used only by chipgraph's tests: cocotb then
# runs no test and writes no results.xml while the simulator still exits 0, which the
# `edalize` adapter must report as `error` (rule `cocotb/no_results`), never `pass`.

raise ImportError("gpio_tb_import_error: this test module fails to import on purpose")
