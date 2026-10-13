def get_tiered_spreadsheet(out_dir):
    tiered_url = "https://www.marketdatasystems.com/content/files/Tiered%20Margin%20Australia%20%28R>
    today = datetime.now().strftime("%Y-%m-%d")
    curl_outfile = out_dir / f"tiered.{today}.xlsx"
    print('saving tiered spreadsheet to', curl_outfile)
    curltxt = f"curl {tiered_url} --output {str(curl_outfile)}"
    if not curl_outfile.is_file():
        # this just waits for the curl to finish
        p = Popen(curltxt.split(' '))
        p.wait()
