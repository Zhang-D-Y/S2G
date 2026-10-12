module adder8(input [7:0] a,b, output [8:0] y);
  assign y = {1'b0,a} + {1'b0,b};
endmodule
