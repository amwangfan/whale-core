# skill: search-and-read
description: 搜索公开资料并读取第一个来源，保留真实回执与链接。
## step: search
tool: search_web
args:
  query: "{input}"
save_as: search
## step: read
tool: fetch_url
args:
  url: "{steps.search.first_url}"
save_as: page
