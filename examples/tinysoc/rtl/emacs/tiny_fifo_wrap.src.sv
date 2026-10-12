// SPDX-License-Identifier: Apache-2.0
//
// tiny_fifo_wrap: tinysoc's wrapper of the vendored FIFO (vendor/vendor_fifo), with the
// project's port names. The IP is never edited; this file maps its ports.
//
// Edit only this source. The generator (rule `digital-rtl/wrapper_gen`, tool `emacs_auto`)
// expands its AUTO comments with emacs verilog-mode and writes rtl/tiny_fifo_wrap.sv;
// the `wrap_check` check fails when the two disagree.

module tiny_fifo_wrap (
  // clock, reset and clear
  /*AUTOINPUT("^i_clk\|^i_rst\|^i_clr")*/
  // write side
  /*AUTOINPUT("^i_push\|^i_data")*/
  /*AUTOOUTPUT("^o_full")*/
  // read side
  /*AUTOINPUT("^i_pop")*/
  /*AUTOOUTPUT("^o_data\|^o_empty")*/
  // others: stays empty while the template maps every IP port
  /*AUTOINPUT*/
  /*AUTOOUTPUT*/
);

  /*AUTOWIRE*/

  // An IP output the wrapper does not use is sunk in an `unused_*` wire, never a port.
  /*AUTO_LISP(setq verilog-auto-output-ignore-regexp "^unused_")*/

  /* vendor_fifo AUTO_TEMPLATE (
      .rst_ni     (i_rst_n),
      .testmode_i (1'b0),
      .err_o      (unused_err),
      .\(.*\)_i   (i_\1[]),
      .\(.*\)_o   (o_\1[]),
  ); */
  vendor_fifo #(
    .Width (8),
    .Depth (4)
  ) u_fifo (/*AUTOINST*/);

endmodule
