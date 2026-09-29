// A module seeded with violations. The expected (line, rule) pairs are asserted in
// tests/checks/test_naming.py::EXPECTED. Some names trip more than one rule on purpose
// (e.g. a bad prefix and a lexical rule), which is realistic.
module bad_top (            // L4  2.1 module      -- not m_qnsc_*/qnsc_*
  input  logic clk_in,     // L5  1.2 port prefix  -- no i_/o_/io_ prefix
  output logic o_data
);

  parameter int width = 8; // L9  2.4 parameter    -- not P_/C_/S_ uppercase
  logic count;             // L10 2.3 signal       -- no r_/w_/mem_ prefix
  logic [3:0] buf_arr [4]; // L11 2.3 signal       -- unpacked dim => memory, not mem_
  logic rstn;              // L12 1.3 active low + 2.3 signal
  logic sig0;              // L13 1.4 index + 2.3 signal
  logic w_clock;           // L14 1.5 vocabulary   -- 'clock' should be 'clk'
  logic BadCase;           // L15 1.1 case + 2.3 signal

  typedef enum logic { idle_st } my_state_e;  // L17 2.4 parameter -- enum member not S_

  m_qnsc_sub bad_u ( .i_clk(clk_in) );        // L19 2.2 instance  -- not u_*

endmodule
