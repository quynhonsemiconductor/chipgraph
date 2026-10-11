module ex_b_flag (
    input logic clk,
    input logic rst_n,
    input logic set,
    input logic clear,
    output logic flag
);


    logic flag_next;
    logic flag_r;


    always_comb
    begin
        flag_next = flag_r;
        if (clear)
        begin
            flag_next = 1'b0;
        end
        else if (set)
        begin
            flag_next = 1'b1;
        end
    end


    always_ff @(posedge clk)
    begin
        if (!rst_n)
        begin
            flag_r <= 1'b0;
        end
        else
        begin
            flag_r <= flag_next;
        end
    end


    assign flag = flag_r;

endmodule
