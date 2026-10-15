#load txt to json for lora

import json
import sys
import os
import time
import tqdm
import pandas as pd

path = "./data/"
year = "2026"

def load_txts_to_pd(path, year):
    json_data = []
    files = os.listdir(path)

    cnt = 0
    df = pd.DataFrame(columns=['id', 'date', 'title', 'content'])
    try:
        df = pd.read_csv(f"./clean_data/RenMin_Daily_{year}.csv", encoding='utf-8')
        df_ids = df['id'].tolist()
    except:
        df_ids = []

    for file in tqdm.tqdm(files):
        if file.endswith(".txt") and file.startswith(year):
            id = file[:-4]
            if id in df_ids:
                continue
            #print file name
            #get top 3 lines as instruction, and the rest as output
            with open(path + file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if len(lines) < 8 and "广告" in "".join(lines):
                continue

            #get instruction
            instruction = ""
            for i in range(3):
                if lines[i].strip() != "" and lines[i].strip() != "\n" and lines[i]!=lines[i-1]:
                    instruction += lines[i].strip() + " "

            if instruction == "" or "责编" in instruction or "版式设计" in instruction or "广告" in instruction or "图片报道" in instruction:
                continue

            #get output
            output = ""
            for i in range(3, len(lines)):
                if lines[i].strip() != "":
                    output += lines[i]


            data = {}
            data['id'] = file[:-4]
            data['date'] = file[:8]
            data['title'] = instruction
            data['content'] = output
            df = pd.concat([df, pd.DataFrame(data, index=[cnt])])
            cnt += 1

    df.to_csv(f"./clean_data/RenMin_Daily_{year}.csv", index=False, encoding='utf-8')


if __name__ == '__main__':
    load_txts_to_pd(path, year)
    print("done")
    #load_txts_to_pd()
    #print("done")
    #load_json_to_pd()
    #print("done")
    #load_pd_to_json()
    #print("done")
    #load_pd_to_txt()
    #print("done")
