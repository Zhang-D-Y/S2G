# Demonstration target, not an owner's production timing requirement.
create_clock -name virtual_io -period 10.0
set_input_delay -clock virtual_io -max 2.0 [all_inputs]
set_input_delay -clock virtual_io -min 0.2 [all_inputs]
set_output_delay -clock virtual_io -max 2.0 [all_outputs]
set_output_delay -clock virtual_io -min 0.2 [all_outputs]
set_driving_cell -lib_cell BUF_X1 -pin Z [all_inputs]
set_load 0.005 [all_outputs]
