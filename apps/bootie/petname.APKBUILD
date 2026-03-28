# https://gitlab.alpinelinux.org/alpine/aports/-/merge_requests/88636
# Maintainer: Sam Day <me@samcday.com>
pkgname=petname
pkgver=2.9
pkgrel=1
pkgdesc="an RFC1178 implementation to generate pronounceable, sometimes even memorable, \"pet names\""
url="https://github.com/dustinkirkland/petname"
arch="noarch"
license="Apache-2.0"
options="!check" # No tests
source="
	$pkgname-$pkgver.tar.gz::https://github.com/dustinkirkland/petname/archive/$pkgver.tar.gz
	"
subpackages="$pkgname-doc"

package() {
	for sz in small medium large; do\
		install -d "$pkgdir/usr/share/petname/$sz"
		install -m0644 usr/share/petname/"$sz"/*.txt "$pkgdir/usr/share/petname/$sz/"
	done
	install -Dm0644 usr/share/petname/*.txt "$pkgdir/usr/share/petname/"

	install -Dm0755 usr/bin/petname "$pkgdir/usr/bin/petname"
	install -Dm0644 usr/share/man/man1/petname.1 "$pkgdir/usr/share/man/man1/petname.1"
}

sha512sums="
8383f44f22a3d200ccb35ff66d03a7daa25aef79ae4675a2fe88e63842c8603f67db1261946c3c491351b3c908d430deab2039c115993c5896cb7cc0bd9ee922  petname-2.9.tar.gz
"
