# %%
import selenium
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options
import time
import os
import pandas as pd
import random
import tqdm
import requests

options = Options()
prefs = {"profile.managed_default_content_settings.images": 2}
options.add_experimental_option("prefs", prefs)

# %%
url = "https://data.eastmoney.com/report/stock.jshtml"
'''
# %%
wd = webdriver.Edge()
wd.get(url)

# %%

df = pd.DataFrame(columns=['股票代码',
 '股票简称',
 '东财评级',
 '评级变动',
 '机构',
 '近一月个股研报数',
 '2024盈利预测',
 '2024市盈率预测',
 '2025盈利预测',
 '2025市盈率预测',
 '行业',
 '日期',
 '报告名称',
 '报告链接',
 '原始文本',
 ] )


# %%
text_last = ""
cnt_chk = 0
repeat = 0
while True:
    time.sleep(1.5)
    if repeat > 10:
        break
    Xpath = '//*[@id="stock_table"]/table'
    table = wd.find_element(By.XPATH, Xpath)
    try:
        table_text = table.text
    except:
        print("table.text error")
        time.sleep(1)
        continue
    if table_text == text_last:
        print("重复")
        time.sleep(1)
        repeat += 1
        continue
    text_last = table_text
    items = table.find_elements(By.TAG_NAME, 'tr')
    for item in items:
        if len(item.find_elements(By.TAG_NAME, 'td')) < 5:
            continue
        values = item.find_elements(By.TAG_NAME, 'td')
        dic = {}
        dic['股票代码'] = values[1].text
        dic['股票简称'] = values[2].text
        dic['报告名称'] = values[4].text
        dic['报告链接'] = values[4].find_element(By.TAG_NAME, 'a').get_attribute('href')
        dic['东财评级'] = values[5].text
        dic['评级变动'] = values[6].text
        dic['机构'] = values[7].text
        dic['近一月个股研报数'] = values[8].text
        dic['2024盈利预测'] = values[9].text
        dic['2024市盈率预测'] = values[10].text
        dic['2025盈利预测'] = values[11].text
        dic['2025市盈率预测'] = values[12].text
        dic['行业'] = values[13].text
        dic['日期'] = values[14].text
        print(dic)
        df = pd.concat([df, pd.DataFrame([dic])], ignore_index=True)
    time.sleep(1)
    try:
        next_page = wd.find_element(By.LINK_TEXT, "下一页")
        next_page.click()
    except:
        print("下一页 error")
        time.sleep(1)
        repeat += 1
        continue
    time.sleep(1)
    print("下一页")
    df.to_excel(f"东财研报_{cnt_chk}.xlsx")
    cnt_chk = (cnt_chk + 1) % 3
    repeat = 0
wd.quit()

# %%
#用报告链接去重

# %%
df2.to_excel('东财研报.xlsx', index=False)
df2

# %%
'''

df2 = None
for i in range(3):
    try:
        df = pd.read_excel(f"东财研报详细_{i}.xlsx")
        if df2 is None:
            df2 = df
        else:
            if len(df) > len(df2):
                df2 = df
    except:
        continue

df2 = df2.drop_duplicates(subset=['报告链接'])
df2

# %%
df2['股票代码'] = df2['股票代码'].apply(lambda x: str(x).zfill(6))
df2['股票代码'].astype(str)
df2

# %%
headers = {
    "sec-ch-ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
}


# %%
import threading
import queue

def download_pdf(pdf_url, pdf_name):
    code = 0
    cnt = 0
    while code != 200:
        try:
            r = requests.get(pdf_url, headers=headers)
            code = r.status_code
        except:
            time.sleep(random.randint(1, 3))
            continue
        cnt += 1
        if cnt > 5:
            print("下载失败")
            return -1
    with open("PDF/"+pdf_name, "wb") as f:
        f.write(r.content)
    return 0

# %%
q_txt_max = 8
q_txt = queue.Queue()
q_pdf = queue.Queue()
wds = [webdriver.Edge(options=options) for i in range(q_txt_max)]
def get_txt(url, i, pdf_name, no_thread):
    wd = wds[no_thread]
    wd.get(url)
    time.sleep(2)
    content = wd.find_element(By.ID, 'ctx-content')
    pdf = wd.find_element(By.CLASS_NAME, 'pdf-link')
    pdf_url = pdf.get_attribute('href')
    txt = content.text
    q_txt.put((i, txt))
    if os.path.exists("PDF/"+pdf_name) is False:
        q_pdf.put((pdf_url, pdf_name))

# %%
q_download = queue.Queue()
q_txt_thread = queue.Queue()
q_txt_cnt = 0
cnt_chk = 0
for i in tqdm.trange(len(df2)):
    url = df2['报告链接'][i]
    text = df2['原始文本'][i]
    pdf_name = f"{url.split('/')[-1].split('.')[0]}_{df2['股票代码'][i]}_{df2['股票简称'][i]}_{df2['机构'][i]}_{df2['日期'][i]}.pdf"
    if type(text) != str or os.path.exists("PDF/"+pdf_name) is False:
        thread = threading.Thread(target=get_txt, args=(url, i, pdf_name, q_txt_cnt))
        thread.start()
        q_txt_thread.put(thread)
        q_txt_cnt = (q_txt_cnt + 1) % q_txt_max
    if q_txt_thread.qsize() >= q_txt_max:
        for _ in range(q_txt_thread.qsize()):
            q_txt_thread.get().join()
    if q_pdf.qsize() >= q_txt_max:
        for _ in range(q_pdf.qsize()):
            pdf_url, pdf_name = q_pdf.get()
            thread = threading.Thread(target=download_pdf, args=(pdf_url, pdf_name))
            thread.start()
            q_download.put(thread)
    if q_txt.qsize() >= q_txt_max:
        for _ in range(q_txt.qsize()):
            i, txt = q_txt.get()
            df2.loc[i, '原始文本'] = txt
        df2.to_excel(f"东财研报详细_{cnt_chk}.xlsx")
        cnt_chk = (cnt_chk + 1) % 3
    if q_download.qsize() >= q_txt_max * 8:
        for _ in range(q_download.qsize()):
            q_download.get().join()

for _ in range(q_txt_thread.qsize()):
    q_txt_thread.get().join()

for _ in range(q_txt.qsize()):
    i, txt = q_txt.get()
    df2.loc[i, '原始文本'] = txt
df2.to_excel('东财研报详细.xlsx', index=False)

for _ in range(q_pdf.qsize()):
    pdf_url, pdf_name = q_pdf.get()
    thread = threading.Thread(target=download_pdf, args=(pdf_url, pdf_name))
    thread.start()
    q_download.put(thread)
for _ in range(q_download.qsize()):
    q_download.get().join()


# %%
for wd in wds:
    wd.quit()
