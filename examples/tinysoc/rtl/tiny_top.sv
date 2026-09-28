// SPDX-License-Identifier: Apache-2.0
// chipgraph example: tiny_top
//
// Instantiates `tiny_timer` and `tiny_gpio` behind a trivial register bus: the top two
// bits of `addr` select the block, the bottom two bits are passed through as that
// block's own register address.

module tiny_top (
    input  logic         clk,
    input  logic          rst_n,
    input  logic [3:0]    addr,
    input  logic           wr_en,
    input  logic [31:0]    wdata,
    output logic [31:0]    rdata,
    input  logic [7:0]     gpio_pin_in,
    output logic [7:0]     gpio_pin_out,
    output logic [7:0]     gpio_pin_dir,
    output logic            timer_irq
);

  localparam logic [1:0] kSelTimer = 2'd0;
  localparam logic [1:0] kSelGpio = 2'd1;

  logic [1:0]  block_sel;
  logic [1:0]  inner_addr;
  logic        timer_wr_en;
  logic        gpio_wr_en;
  logic [31:0] timer_rdata;
  logic [31:0] gpio_rdata;

  assign block_sel  = addr[3:2];
  assign inner_addr = addr[1:0];

  assign timer_wr_en = wr_en && (block_sel == kSelTimer);
  assign gpio_wr_en  = wr_en && (block_sel == kSelGpio);

  always_comb begin
    unique case (block_sel)
      kSelTimer: rdata = timer_rdata;
      kSelGpio:  rdata = gpio_rdata;
      default:   rdata = 32'd0;
    endcase
  end

  tiny_timer u_timer (
      .clk   (clk),
      .rst_n (rst_n),
      .addr  (inner_addr),
      .wr_en (timer_wr_en),
      .wdata (wdata),
      .rdata (timer_rdata),
      .irq   (timer_irq)
  );

  tiny_gpio u_gpio (
      .clk     (clk),
      .rst_n   (rst_n),
      .addr    (inner_addr),
      .wr_en   (gpio_wr_en),
      .wdata   (wdata),
      .rdata   (gpio_rdata),
      .pin_in  (gpio_pin_in),
      .pin_out (gpio_pin_out),
      .pin_dir (gpio_pin_dir)
  );

endmodule
