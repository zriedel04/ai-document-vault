# Writes the property tables of backend/src/vault/answers/graphemes.py (Perl 5.38, Unicode 15.0.0):
# perl grapheme_tables.pl
use strict; use warnings;
use Unicode::UCD qw(prop_invlist);
die "need Unicode 15.0.0" unless Unicode::UCD::UnicodeVersion() eq "15.0.0";
sub ranges { my @l = @_; my @r; for (my $i=0; $i<@l; $i+=2) { my $end = defined $l[$i+1] ? $l[$i+1]-1 : 0x10FFFF; push @r, sprintf("(0x%04X, 0x%04X)", $l[$i], $end); } return @r; }
for my $p (["CONTROL","GCB=Control"],["EXTEND","GCB=Extend"],["PREPEND","GCB=Prepend"],["SPACING_MARK","GCB=SpacingMark"],["EXTENDED_PICTOGRAPHIC","Extended_Pictographic"]) {
  my @r = ranges(prop_invlist($p->[1]));
  print "$p->[0]: Final[Ranges] = (\n";
  while (my @row = splice(@r, 0, 4)) { print "    ", join(", ", @row), ",\n"; }
  print ")\n";
}
for my $v (qw(CR LF ZWJ Regional_Indicator L V T)) { print "# $v: ", join(" ", ranges(prop_invlist("GCB=$v"))), "\n"; }
