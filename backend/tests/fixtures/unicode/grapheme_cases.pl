# Writes grapheme_break_cases.txt (without its header) with Perl 5.38 (Unicode 15.0.0) \X:
# perl grapheme_cases.pl
use strict; use warnings;
use Unicode::UCD ();
die "need Unicode 15.0.0" unless Unicode::UCD::UnicodeVersion() eq "15.0.0";
# One or two samples of every Grapheme_Cluster_Break value, Extended_Pictographic, and Other.
my @samples = (0x000D, 0x000A, 0x0000, 0x00AD, 0x200B, 0x2028, 0x0308, 0x0300, 0x093E, 0x0BBE,
  0xFE0F, 0x1F3FB, 0x200D, 0x1F1E6, 0x1F1FA, 0x0600, 0x0D4E, 0x0903, 0x0E33, 0x1100, 0x1160,
  0x11A8, 0xAC00, 0xAC01, 0x0020, 0x0061, 0x0915, 0x094D, 0x00A9, 0x1F476, 0x2640, 0x0B47,
  0x0B3E, 0x0CD5, 0x0378, 0xE0001, 0xE0020);
my @pool = (@samples, 0x0065, 0x0301, 0x0E01, 0x1F3FF, 0x261D, 0x0644, 0x0627);
sub line { my @cps = @_; my $s = join "", map { chr } @cps; my @g = $s =~ /(\X)/gs;
  my @out = ("\x{F7}"); for my $g (@g) { my $first = 1; for my $c (split //, $g) { push @out, "\x{D7}" unless $first; $first = 0; push @out, sprintf("%04X", ord $c); } push @out, "\x{F7}"; }
  return join(" ", @out) . "\n"; }
binmode STDOUT, ":encoding(UTF-8)";
for my $a (@samples) { for my $b (@samples) { print line($a, $b); print line($a, 0x0308, $b); } }
for my $seq ([0x1F476, 0x1F3FF, 0x0308, 0x200D, 0x1F476, 0x1F3FF], [0x0061, 0x200D, 0x2640],
  [0x2701, 0x200D, 0x2701], [0x1F1E6, 0x1F1E6, 0x1F1E6], [0x0061, 0x1F1E6, 0x1F1E6, 0x1F1E6, 0x0062],
  [0x1F1E6, 0x200D, 0x1F1E6], [0x0915, 0x094D, 0x0924], [0x1100, 0x1161, 0x11A8, 0x11A8],
  [0x0600, 0x0600, 0x0061], [0x00A9, 0xFE0F, 0x200D, 0x1F476]) { print line(@$seq); }
srand(29);
for (1..3000) { my $n = 1 + int(rand(7)); print line(map { $pool[int(rand(@pool))] } 1..$n); }
