# hub router

This directory contains scripts/files necessary to build a custom OpenWRT image using [Image Builder][].

## Building

```
./build-image.sh <name> <platform> <target> <profile>

# Example to build for an AVM 4040
./build-image.sh ipq40xx generic avm_fritzbox-4040

# And for an RT-AX53U:
./build-image.sh ramips mt7621 asus_rt-ax53u
```

[Image Builder]: https://openwrt.org/docs/guide-user/additional-software/imagebuilder
