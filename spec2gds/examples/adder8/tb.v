module tb;
  reg [7:0] a,b;
  wire [8:0] y;
  integer i,j,checks;
  adder8 dut(a,b,y);
  initial begin
    checks=0;
    for(i=0;i<256;i=i+1) for(j=0;j<256;j=j+1) begin
      a=i; b=j; #1;
      if (y !== (i+j)) $fatal(1,"S2G_FAIL a=%d b=%d y=%d",i,j,y);
      checks=checks+1;
    end
    $display("S2G_PASS checks=%0d failures=0",checks);
    $finish;
  end
endmodule
