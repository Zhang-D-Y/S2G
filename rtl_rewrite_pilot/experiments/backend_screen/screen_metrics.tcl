# Instrument only: no changes to the default repair policy.
proc screen_snapshot {label} {
  set prefix "$::env(RESULTS_DIR)/screen_$label"
  utl::push_metrics_stage "screen_${label}__{}"
  report_metrics 0 "screen_$label" true false
  utl::pop_metrics_stage
  write_db "$prefix.odb"
  write_verilog -remove_cells {sky130_fd_sc_hd__tapvpwrvgnd* sky130_fd_sc_hd__fill* sky130_fd_sc_hd__decap*} "$prefix.v"
  set block [ord::get_db_block]
  set f [open "$prefix.instances.tsv" w]
  puts $f "name\tmaster\ttype\tarea_um2"
  set scale [$block getDbUnitsPerMicron]
  foreach inst [$block getInsts] {
    set master [$inst getMaster]
    set area [expr {double([$master getWidth])*[$master getHeight]/$scale/$scale}]
    puts $f "[$inst getName]\t[$master getName]\t[$master getType]\t$area"
  }
  close $f
  set f [open "$prefix.pins.tsv" w]
  foreach bt [$block getBTerms] {
    foreach bp [$bt getBPins] {
      foreach box [$bp getBoxes] {
        puts $f "[$bt getName]\t[$bp getPlacementStatus]\t[[$box getTechLayer] getName]\t[$box xMin]\t[$box yMin]\t[$box xMax]\t[$box yMax]"
      }
    }
  }
  close $f
}

# Wrap each high-level physical repair command, preserving arguments and result.
# Inventory deltas are net edits per call, not internal attempts that were rolled back.
set ::screen_serial 0
proc screen_wrap {command} {
  if {[llength [info commands $command]] == 0 || [llength [info commands screen_original_$command]]} {return}
  rename $command screen_original_$command
  proc $command {args} [string map [list @COMMAND@ $command] {
    incr ::screen_serial
    set label "repair${::screen_serial}_@COMMAND@"
    screen_snapshot "${label}_before"
    set t0 [clock milliseconds]
    set code [catch {uplevel 1 [list screen_original_@COMMAND@ {*}$args]} result opts]
    set elapsed [expr {[clock milliseconds]-$t0}]
    set f [open "$::env(RESULTS_DIR)/screen_repair_events.tsv" a]
    puts $f "$label\t$elapsed\t$code\t$args"
    close $f
    if {$code != 0} {return -options $opts $result}
    screen_snapshot "${label}_after"
    return $result
  }]
}
foreach cmd {repair_design repair_timing buffer_ports repair_tie_fanout repair_antennas} {screen_wrap $cmd}
