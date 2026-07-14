# common/router

This tree contains files that are incorporated into all OpenWrt images that are built elsewhere in this repo.

`ipxe-files.txt` pins the exact pre-OS iPXE binaries embedded in router images
that provide PXE. Updating either binary is a deliberate supply-chain review:
fetch it over HTTPS, verify the upstream context, update the digest, and rebuild
every affected router image. The build refuses a cached or downloaded hash
mismatch. A profile containing `disable-ipxe` omits the binaries entirely; use
that only when its DHCP/TFTP/PXE path is deliberately absent.
