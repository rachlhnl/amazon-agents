#!/bin/zsh
# Files Seller Central and Quartile exports from ~/Downloads into the data folder, so a
# download is all a person has to do. Run by launchd whenever Downloads changes
# (com.dagacci.file-exports.plist) and at the start of refresh.sh.
#   Brand Analytics weekly CSVs        -> seller_central/ (Amazon's file names kept)
#   Voice of the Customer summary      -> seller_central/ (the previous one is archived)
#   All Orders / FBA inventory / listings / business reports -> data/seller-<date>/
#   Quartile ASIN export (.xlsx)       -> data/quartile-asin-<date>.xlsx
setopt null_glob
ROOT="${DAGACCI_DATA:-$HOME/Documents/dagacci}"
DL="${DOWNLOADS:-$HOME/Downloads}"
SC="$ROOT/seller_central"
today="$(date +%F)"
mkdir -p "$SC" "$ROOT/data" "$ROOT/archive/seller_central-superseded"
moved=0
note() { echo "filed: $1 -> $2"; moved=$((moved + 1)); }
# A file can match two patterns; each loop body skips what an earlier move already took.

for f in "$DL"/Trends_*_Weekly_*.csv "$DL"/US_Search_Query_Performance_*.csv \
         "$DL"/US_Repeat_Purchase_Behavior_*.csv "$DL"/US_Market_Basket_Analysis_*.csv; do
  [[ -f "$f" ]] || continue
  mv -f "$f" "$SC/" && note "${f:t}" "seller_central/"
done
for f in "$DL"/Listing_Summary_Report*.csv; do
  [[ -f "$f" ]] || continue
  [[ -f "$SC/Listing_Summary_Report.csv" ]] && \
    mv -f "$SC/Listing_Summary_Report.csv" "$ROOT/archive/seller_central-superseded/Listing_Summary_Report-$today.csv"
  mv -f "$f" "$SC/Listing_Summary_Report.csv" && note "${f:t}" "seller_central/Listing_Summary_Report.csv"
done
for f in "$DL"/*[Oo]rders*.txt "$DL"/*[Ii]nventory*.txt "$DL"/*[Ll]istings*.txt "$DL"/*[Bb]usiness*[Rr]eport*.csv; do
  [[ -f "$f" ]] || continue
  mkdir -p "$ROOT/data/seller-$today"
  mv -f "$f" "$ROOT/data/seller-$today/" && note "${f:t}" "data/seller-$today/"
done
for f in "$DL"/*[Qq]uartile*.xlsx "$DL"/*ASIN*[Aa]nalysis*.xlsx; do
  [[ -f "$f" ]] || continue
  mv -f "$f" "$ROOT/data/quartile-asin-$today.xlsx" && note "${f:t}" "data/quartile-asin-$today.xlsx"
done
(( moved )) && echo "$(date '+%F %T') filed $moved export(s)"
exit 0
