# Observations only: original commands/arguments/results are preserved.
proc s2g_inventory {path} {
  set f [open $path w]
  foreach inst [[ord::get_db_block] getInsts] {
    puts $f "[$inst getName]\t[[$inst getMaster] getName]"
  }
  close $f
}
set ::s2g_serial 0
proc s2g_wrap {command} {
  if {[llength [info commands $command]] == 0 || [llength [info commands s2g_original_$command]]} {return}
  rename $command s2g_original_$command
  proc $command {args} [string map [list @COMMAND@ $command] {
    incr ::s2g_serial
    set label "[file rootname [file tail [info script]]]_[pid]_${::s2g_serial}_@COMMAND@"
    set prefix "$::env(RESULTS_DIR)/s2g_$label"
    s2g_inventory "${prefix}_before.tsv"
    set start [clock milliseconds]
    set code [catch {uplevel 1 [list s2g_original_@COMMAND@ {*}$args]} result opts]
    set elapsed [expr {[clock milliseconds]-$start}]
    s2g_inventory "${prefix}_after.tsv"
    set f [open "$::env(RESULTS_DIR)/s2g_repair_events.tsv" a]
    puts $f "$label\t@COMMAND@\t$elapsed\t$code\t$args"
    close $f
    return -options $opts $result
  }]
}
foreach command {repair_design repair_timing buffer_ports repair_tie_fanout repair_antennas} {s2g_wrap $command}
