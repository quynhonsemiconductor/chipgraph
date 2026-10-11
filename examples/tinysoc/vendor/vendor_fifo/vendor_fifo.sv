// SPDX-License-Identifier: Apache-2.0
//
// vendor_fifo: a small synchronous FIFO, written for the chipgraph tinysoc example as a
// stand-in for a third-party IP. Treat it as vendored code that must not be edited:
// vendor/MANIFEST.sha256 records the hash of every file under vendor/, and the
// `vendor_guard` check fails when one changes. The project wraps it instead
// (rtl/emacs/tiny_fifo_wrap.src.sv), with the IP's own port names (`*_i`, `*_o`, `rst_ni`).
//
// Width bits per entry, Depth entries (a power of two). A push when full or a pop when
// empty is dropped and raises err_o for that cycle. clr_i empties the FIFO; testmode_i
// makes it look empty to the reader.

module vendor_fifo #(
  parameter int unsigned Width = 8,
  parameter int unsigned Depth = 4
) (
  input  logic             clk_i,
  input  logic             rst_ni,
  input  logic             clr_i,
  input  logic             testmode_i,
  // write side
  input  logic             push_i,
  input  logic [Width-1:0] data_i,
  output logic             full_o,
  // read side
  input  logic             pop_i,
  output logic [Width-1:0] data_o,
  output logic             empty_o,
  // a dropped push or pop
  output logic             err_o
);

  localparam int unsigned AddrW = (Depth > 1) ? $clog2(Depth) : 1;

  logic [Width-1:0] mem_q [Depth];
  logic [AddrW-1:0] wptr_q, rptr_q;
  logic [AddrW:0]   count_q;
  logic             full, empty, do_push, do_pop;

  assign full    = (count_q == (AddrW + 1)'(Depth));
  assign empty   = (count_q == '0);
  assign do_push = push_i && !full;
  assign do_pop  = pop_i && !empty;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      wptr_q  <= '0;
      rptr_q  <= '0;
      count_q <= '0;
    end else if (clr_i) begin
      wptr_q  <= '0;
      rptr_q  <= '0;
      count_q <= '0;
    end else begin
      if (do_push) wptr_q <= wptr_q + AddrW'(1);
      if (do_pop)  rptr_q <= rptr_q + AddrW'(1);
      if (do_push && !do_pop) count_q <= count_q + (AddrW + 1)'(1);
      else if (do_pop && !do_push) count_q <= count_q - (AddrW + 1)'(1);
    end
  end

  always_ff @(posedge clk_i) begin
    if (do_push) mem_q[wptr_q] <= data_i;
  end

  assign full_o  = full;
  assign empty_o = empty || testmode_i;
  assign data_o  = mem_q[rptr_q];
  assign err_o   = (push_i && full && !pop_i) || (pop_i && empty);

endmodule
