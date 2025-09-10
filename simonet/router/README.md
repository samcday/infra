# simonet router

A custom OpenWRT image is built from this tree using [Image Builder][].

## Building

From the root of the repo:

```
scripts/build-router-image.sh simonet/router <platform> <target> <profile>

# Example to build for an AVM 4040
scripts/build-router-image.sh simonet/router ipq40xx generic avm_fritzbox-4040

# And for an RT-AX53U:
scripts/build-router-image.sh simonet/router ramips mt7621 asus_rt-ax53u
```

[Image Builder]: https://openwrt.org/docs/guide-user/additional-software/imagebuilder
