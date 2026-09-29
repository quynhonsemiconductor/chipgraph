// SPDX-License-Identifier: Apache-2.0
// A simple SystemVerilog package with type definitions.

package types_pkg;

   typedef struct packed {
      logic [7:0] data;
      logic [3:0] addr;
      logic valid;
   } data_t;

   parameter int MAX_DEPTH = 256;

endpackage : types_pkg
