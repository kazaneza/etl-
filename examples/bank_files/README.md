# Bank files

Four jobs over made-up bank files in `landing/bank/`: an MT940 statement, a camt.053
statement, a card processor's daily file read with a layout, and a BAI2 cash report.
Every file here is invented: BICs use the country code ZZ, accounts start with DEMO, and
card numbers were generated already masked.

    etlplus detect examples/bank_files/landing/bank/cash_report.bai
    etlplus --project examples/bank_files run statement_mt940

The landing files were written by `etlplus.formats.samples.write_all(folder, date(2026, 11, 1))`;
run it again to get the same bytes, or with another date to get fresh ones.
