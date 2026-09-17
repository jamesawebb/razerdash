I have a home office with Razer Blackwidow V4 keyboard, mouse and KVM-enabled
monitor.  The monitor is connected to a work laptop and personal laptop, Quark.
I have an additional NUC server Cobra running various services as Docker
containers.  Among these services is metrics aggregator Prometheus.

I have installed the OpenRazer stack on Quark and work laptop.

Design a system that modifies the colour of the LEDs on the keyboard based on
different Prometheus metrics.  I will treat different rows and coloumns on the
keyboard as horizontal and vertical bars respectively - henceforth referred to
as a "key group".  Both the number of keys and/or key colour in a given key
group may be used to illustrate a given metric.

I would like to be able to easily make changes via a configuration file (YAML
perferred).  A configuration would control:
- A key group
- The target metric
- The lighting method

Some suggested key groups to add as placeholders in the configuration:
- The set of vertical macro keys M1-M5 (M6 is physically distant).
- The set of horizontal, numeric keys 1-9 and 0.
- Three groups of horizontal "function keys", grouped by physical proximity;
  F1-F4, F5-F8, F9-F12.  These can potentially be combined into a single group
  F1-F12. 

Ask any clarifying question.