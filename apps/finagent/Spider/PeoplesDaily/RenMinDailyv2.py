
import requests
import bs4
import os
import datetime
import time

def fetchUrl(url):
	'''
	功能：访问 url 的网页，获取网页内容并返回
	参数：目标网页的 url
	返回：目标网页的 html 内容
	'''

	headers = {
		'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
		'user-agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/68.0.3440.106 Safari/537.36',
	}

	r = requests.get(url,headers=headers)
	if r.status_code != 200:
		print('访问失败', r.status_code)
		return None
	else:
		r.raise_for_status()
		r.encoding = r.apparent_encoding
		return r.text

def getPageList(year, month, day):
	'''
	功能：获取当天报纸的各版面的链接列表
	参数：年，月，日
 url = 'http://paper.people.com.cn/rmrb/pc/layout/202412/04/node_01.html'
	'''
	url = 'http://paper.people.com.cn/rmrb/pc/layout/' + year + month + '/' + day + '/node_01.html'
	res = requests.get(url)
	res.raise_for_status()
	#parse page content
	soup = bs4.BeautifulSoup(res.text, 'html.parser')
	#find all page links
	page_links = soup.find_all('a', id='pageLink')
	linkList = []
	#find all page links and content
	for page_link in page_links:
		href = page_link.get('href')
		text = page_link.get_text()
		text = text.encode('latin-1').decode('utf-8')
		if '广告' in text:
			continue
		pageUrl = 'http://paper.people.com.cn/rmrb/pc/layout/' + year + month + '/' + day + '/' + href
		page_no = href.split('_')[1].split('.')[0]
		pageUrl = pageUrl.replace('../../../', '')
		# Append the page URL and page number as a tuple
		linkList.append((pageUrl, page_no))

	return linkList

def getTitleList(pageUrl):
    """
    功能：获取页面中所有新闻标题及其链接
    参数：pageUrl - 页面链接
    返回：新闻标题和链接的列表
    """
    res = requests.get(pageUrl)
    res.raise_for_status()
    # Parse page content
    soup = bs4.BeautifulSoup(res.text, 'html.parser')
    # Find all news lists
    news_list = soup.find_all('ul', class_='news-list')
    # Initialize the list to store links and titles
    linkList = []
    for news in news_list:
        # Find all li tags
        li_tags = news.find_all('li')
        for li in li_tags:
            # Get href attribute
            href = li.a.get('href')
            # Get text and decode it
            text = li.a.get_text()
            text = text.encode('latin-1', 'ignore').decode('utf-8', errors='ignore')
            # Construct the full URL
            full_url = 'http://paper.people.com.cn/rmrb/pc/' + href.replace('../../../', '')
            # Append the title and URL as a tuple
            linkList.append(full_url)
    return linkList

def getContent(html):
    '''
    功能：解析 HTML 网页，获取新闻的文章内容
    参数：html 网页内容
    '''
    res = requests.get(html)
    res.raise_for_status()
    # Parse page content
    soup = bs4.BeautifulSoup(res.text, 'html.parser')
    # Find the main content of the article
    content = soup.find('div', class_='article')
    # Extract the text from the content
    if content:
        text = content.get_text()
        # Decode the text
        text = text.encode('latin-1').decode('utf-8', errors='ignore')
        return text
    else:
        return None

def saveFile(content, path, filename):
	'''
	功能：将文章内容 content 保存到本地文件中
	参数：要保存的内容，路径，文件名
	'''
	# 如果没有该文件夹，则自动生成
	if not os.path.exists(path):
		os.makedirs(path)

	# 保存文件
	with open(path + filename, 'w', encoding='utf-8') as f:
		f.write(content)

def download_rmrb(year, month, day, destdir):
	'''
	功能：爬取《人民日报》网站 某年 某月 某日 的新闻内容，并保存在 指定目录下
	参数：年，月，日，文件保存的根目录
	'''
	pageList = getPageList(year, month, day)
	for page, pageNo in pageList:
		titleList = getTitleList(page)
		for url_id, url in enumerate(titleList):

			content = getContent(url)

			#titleNo = temp[0] if int(temp[0]) >= 10 else '0' + temp[0]
			titleNo = str(url_id + 1) if url_id + 1 >= 10 else '0' + str(url_id + 1)
			path = destdir + '/'
			fileName = year + month + day + '-' + pageNo + '-' + titleNo + '.txt'

			# 保存文件
			saveFile(content, path, fileName)


def gen_dates(b_date, days):
	day = datetime.timedelta(days = 1)
	for i in range(days):
		yield b_date + day * i


def get_date_list(beginDate, endDate):
	"""
	获取日期列表
	:param start: 开始日期
	:param end: 结束日期
	:return: 开始日期和结束日期之间的日期列表
	"""

	start = datetime.datetime.strptime(beginDate, "%Y%m%d")
	end = datetime.datetime.strptime(endDate, "%Y%m%d")

	data = []
	for d in gen_dates(start, (end-start).days):
		data.append(d)

	return data


if __name__ == '__main__':
	'''
	主函数：程序入口
	'''
	# 输入起止日期，爬取之间的新闻
	beginDate = '20260424'
	endDate = '20260519'
	data = get_date_list(beginDate, endDate)

	for d in data:
		year = str(d.year)
		month = str(d.month) if d.month >=10 else '0' + str(d.month)
		day = str(d.day) if d.day >=10 else '0' + str(d.day)
		download_rmrb(year, month, day, './data')
		print("爬取完成：" + year + month + day)
#         time.Sleep(3)        # 怕被封 IP 爬一爬缓一缓，爬的少的话可以注释掉
