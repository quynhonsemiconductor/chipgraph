// SPDX-License-Identifier: Apache-2.0
// A simple FSM using typedef enum and case.

module fsm_controller
  (input  logic clk,
   input  logic rst_n,
   input  logic start,
   input  logic done,
   output logic active);

   typedef enum logic [1:0] {
      IDLE   = 2'b00,
      ACTIVE = 2'b01,
      WAIT   = 2'b10
   } state_e;

   state_e state_q, state_d;

   // State machine
   always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n)
        state_q <= IDLE;
      else
        state_q <= state_d;
   end

   always_comb begin
      state_d = state_q;
      active = 1'b0;
      case (state_q)
         IDLE: begin
            if (start)
              state_d = ACTIVE;
         end
         ACTIVE: begin
            active = 1'b1;
            if (done)
              state_d = WAIT;
         end
         WAIT: begin
            state_d = IDLE;
         end
         default: state_d = IDLE;
      endcase
   end

endmodule
