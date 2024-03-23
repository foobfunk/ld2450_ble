# ld2450_ble
Home assistant integration for LD2450 presence sensor ove Bluetooth

Home Assistant has an official integration for Hi-Link LD2410 distance sensor over Bluetooth (https://www.home-assistant.io/integrations/ld2410_ble/)

This is based on a python class managing sensor connection and data (https://github.com/930913/ld2410-ble)

The newer LD2450 also publishes data over bluetooth, so i modified the python class and the integration code to manage it.

Eventually, it works (more or less..)

![image](https://github.com/MassiPi/ld2450_ble/assets/2384381/19af24d5-2f2c-47e7-b040-351e008fa910)

Still no idea on how to send config commands (like changing from single target mode to multi target mode) but well, this is just the first try..
