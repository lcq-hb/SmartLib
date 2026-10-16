#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartLib 书目数据生成器（纯标准库，零依赖）

用途
    §1.1 的规模基线是 58 万种 / 120 万册，检索方案 Q1~Q5 的对比必须在这个量级
    才有意义。本脚本负责造出"有真实感"的书目与库存数据。

产物（默认写到 deploy/sql/generated/，该目录已在 .gitignore 中）
    --format sql   books_bulk.sql          book / book_stock 两段 INSERT
    --format csv   books_bulk.csv          book_stock_bulk.csv

典型用法
    # 冒烟：只生成 200 条，确认脚本能跑通
    python scripts/gen_books.py --count 200 --out deploy/sql/generated/

    # 正式压测数据集：20 万种书目（默认值）
    python scripts/gen_books.py --count 200000

    # 需要 CSV 时
    python scripts/gen_books.py --count 200000 --format csv

    # 生成 120 万册的规模：每本书平均 2 册
    python scripts/gen_books.py --count 580000 --avg-copies 2

设计要点与取舍
    1) 【纯标准库】random / argparse / os / sys / datetime，不用 Faker。
       理由：评审机/CI 上装不了包也能跑；简历项目不该因为数据生成器而多一个依赖。
    2) 【可复现】--seed 固定随机源。同一 seed 生成的数据完全相同，
       这样"我压测时用的是哪份数据"可以写进文档并复现。
    3) 【ISBN 合法】按 ISBN-13 规则算校验位（加权 1/3 交替 + mod 10），
       可以被真实校验器接受，不是随便凑的 13 位数字。
    4) 【不写死自增 id】book_stock 用 (SELECT id FROM book WHERE isbn='...') 关联，
       避免和 backend 侧 data.sql 的自增序列打架。
       为了让 --start-id 可预测，MySQL 侧的约定是：book 表在此之前为空，
       自增从 1 开始（种子数据会占用一些 id，见 --start-id）。
    5) 【先灌数据后建索引】ngram 全文索引（见 deploy/sql/mysql-production-extras.sql）
       会显著拖慢批量插入（写放大）。正确顺序是：先灌本脚本的数据，再建 FULLTEXT。
    6) 【分批 INSERT】每 500 行一条 INSERT 语句（--batch-size），
       20 万行约 400 条语句，明显快于逐行插入，又不会撑爆 max_allowed_packet。
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from datetime import datetime

# =============================================================================
# 词表：中文书名/作者/出版社/分类
# 目标不是"穷尽"，而是让随机组合看起来像真实馆藏：
#   * 书名 = 前缀(可选) + 主题词 + 后缀
#   * 每种主题绑定一个中图法分类号与馆藏楼层，位置信息自洽
# =============================================================================

# 书名后缀：决定"这是一本什么形态的书"
TITLE_SUFFIXES = [
    "入门与实践", "从入门到精通", "实战", "原理与实现", "设计与实现",
    "核心技术与案例", "进阶指南", "最佳实践",
    "精讲", "详解", "教程", "导论", "概论", "基础", "高级编程",
    "项目实战", "性能优化", "架构之道", "疑难解析", "面试宝典",
    "案例精析", "习题解答", "实验指导", "技术内幕", "权威指南",
    "快速上手", "深度剖析", "全解析", "一本通", "手册",
]

# 书名前缀：修饰强度/受众，让同一主题词能裂变出多个不同书名
TITLE_PREFIXES = [
    "", "", "", "", "",  # 空字符串占多数：大多数书名没有前缀
    "深入理解", "图解", "重学", "现代", "大话", "零基础学",
    "高性能", "分布式", "云原生", "新一代", "简明", "实用",
    "循序渐进", "边做边学", "每天 5 分钟学",
]

# 按分类组织的主题词 / 出版社 / 作者 / 馆藏位置
# code 是中图法分类号，用于生成自洽的索书号（如 TP312/1287）
CATEGORY_PROFILES = [
    {
        "code": "TP",
        "category_name": "计算机与信息技术",
        "topics": [
            "Java", "Python", "Go", "Rust", "C++", "Spring Boot", "Spring Cloud",
            "MyBatis", "MySQL", "PostgreSQL", "Redis", "MongoDB", "Elasticsearch",
            "Kafka", "RocketMQ", "Docker", "Kubernetes", "微服务", "分布式系统",
            "高并发系统", "算法", "数据结构", "操作系统", "计算机网络", "编译原理",
            "数据库系统", "设计模式", "重构", "领域驱动设计", "单元测试",
            "性能调优", "Linux 内核", "网络安全", "密码学", "机器学习",
            "深度学习", "自然语言处理", "计算机视觉", "推荐系统", "知识图谱",
            "大语言模型", "检索增强生成", "向量数据库", "云原生可观测性",
            "DevOps", "持续集成", "前端工程化", "Web 性能", "大数据处理",
            "Hadoop", "Spark", "Flink", "数据仓库", "数据湖", "敏捷开发",
        ],
        "publishers": [
            "机械工业出版社", "人民邮电出版社", "电子工业出版社",
            "清华大学出版社", "中国水利水电出版社", "华中科技大学出版社",
            "O'Reilly 中文版", "图灵教育",
        ],
        "authors": [
            "周志明", "张三丰", "李承远", "王宏", "刘伟", "陈硕", "郑曦",
            "Robert C. Martin", "Martin Fowler", "Joshua Bloch", "Brian Goetz",
            "Craig Walls", "Chris Richardson", "Neal Ford", "Kyle Simpson",
            "徐亚峰", "朱忠华", "杨冠宝", "高洪岩", "汪云飞", "翟永超",
        ],
        "floors": ["三楼东区", "三楼西区", "四楼东区"],
        "class_prefix": "TP",
        "tag_pool": [
            "编程", "架构", "分布式", "缓存", "数据库", "性能", "并发",
            "源码", "面试", "工程实践", "云原生", "算法", "人工智能",
        ],
    },
    {
        "code": "O1",
        "category_name": "数学",
        "topics": [
            "高等数学", "数学分析", "线性代数", "高等代数", "概率论", "数理统计",
            "离散数学", "数值分析", "复变函数", "实变函数", "泛函分析",
            "常微分方程", "偏微分方程", "抽象代数", "拓扑学", "数论",
            "组合数学", "图论", "运筹学", "数学建模", "最优化方法",
            "随机过程", "矩阵论", "解析几何", "微分几何", "博弈论",
        ],
        "publishers": [
            "高等教育出版社", "北京大学出版社", "科学出版社",
            "清华大学出版社", "复旦大学出版社", "上海科学技术出版社",
            "人民教育出版社", "中国科学技术大学出版社",
        ],
        "authors": [
            "张筑生", "盛骤", "谢式千", "潘承洞", "北京大学数学系",
            "同济大学数学系", "华东师范大学数学系", "陈纪修", "卓里奇",
            "Walter Rudin", "Kenneth H. Rosen", "Sheldon M. Ross",
            "Gilbert Strang", "李尚志", "丘维声", "龚昇", "王萼芳",
        ],
        "floors": ["四楼西区", "四楼东区", "二楼西区"],
        "class_prefix": "O",
        "tag_pool": ["数学", "证明", "函数", "矩阵", "概率", "统计", "建模", "计算"],
    },
    {
        "code": "F2",
        "category_name": "经济管理",
        "topics": [
            "经济学原理", "微观经济学", "宏观经济学", "管理学", "市场营销",
            "财务管理", "会计学", "金融学", "投资学", "国际贸易",
            "货币银行学", "计量经济学", "组织行为学", "战略管理",
            "运营管理", "人力资源管理", "创业管理", "电子商务",
            "供应链管理", "证券投资", "公司理财", "审计学",
        ],
        "publishers": [
            "中国人民大学出版社", "机械工业出版社", "中信出版社",
            "清华大学出版社", "商务印书馆", "上海财经大学出版社",
            "东北财经大学出版社", "北京大学出版社",
        ],
        "authors": [
            "斯蒂芬·罗宾斯", "N. Gregory Mankiw", "黄达", "吴晓求",
            "Michael Porter", "Peter F. Drucker", "Philip Kotler",
            "Stephen A. Ross", "荆新", "王化成", "刘力", "张新民",
            "保罗·萨缪尔森", "Richard A. Brealey",
        ],
        "floors": ["二楼南区", "二楼北区", "六楼西区"],
        "class_prefix": "F",
        "tag_pool": ["经济", "管理", "金融", "投资", "营销", "战略", "案例", "会计"],
    },
    {
        "code": "B0",
        "category_name": "哲学宗教",
        "topics": [
            "西方哲学史", "中国哲学史", "马克思主义哲学", "伦理学", "美学",
            "逻辑学", "宗教学", "心理学导论", "社会心理学", "认知心理学",
            "发展心理学", "哲学导论", "科学哲学", "现象学", "存在主义",
            "儒家思想", "道家思想", "佛学概论", "批判性思维",
        ],
        "publishers": [
            "商务印书馆", "生活·读书·新知三联书店", "北京大学出版社",
            "中国人民大学出版社", "中华书局", "上海译文出版社",
            "华东师范大学出版社", "华夏出版社",
        ],
        "authors": [
            "伯特兰·罗素", "冯友兰", "胡适", "柏拉图", "亚里士多德",
            "Immanuel Kant", "Friedrich Nietzsche", "Sigmund Freud",
            "Carl R. Rogers", "戴维·迈尔斯", "理查德·道金斯",
            "周国平", "叶秀山", "邓晓芒", "陈来",
        ],
        "floors": ["六楼东区", "六楼西区", "五楼东区"],
        "class_prefix": "B",
        "tag_pool": ["哲学", "思想", "伦理", "心理学", "逻辑", "宗教", "经典", "通识"],
    },
    {
        "code": "I2",
        "category_name": "中国文学",
        "topics": [
            "红楼梦", "西游记", "水浒传", "三国演义", "平凡的世界", "活着",
            "白鹿原", "围城", "边城", "骆驼祥子", "四世同堂", "家",
            "春", "秋", "许三观卖血记", "兄弟", "长恨歌", "丰乳肥臀",
            "红高粱家族", "一句顶一万句", "繁花", "人世间",
            "唐诗三百首", "宋词鉴赏", "古文观止", "世说新语",
            "中国现当代文学", "当代小说选", "散文精选",
        ],
        "publishers": [
            "人民文学出版社", "作家出版社", "上海文艺出版社",
            "北京十月文艺出版社", "译林出版社", "江苏凤凰文艺出版社",
            "浙江文艺出版社", "长江文艺出版社", "中华书局",
        ],
        "authors": [
            "曹雪芹", "路遥", "余华", "陈忠实", "钱锺书", "沈从文",
            "老舍", "巴金", "莫言", "王安忆", "刘震云", "金宇澄",
            "梁晓声", "汪曾祺", "阿城", "苏童", "迟子建", "刘慈欣",
        ],
        "floors": ["五楼北区", "五楼中区", "五楼东区"],
        "class_prefix": "I",
        "tag_pool": ["小说", "散文", "诗歌", "古典文学", "当代文学", "名著", "获奖作品"],
    },
    {
        "code": "H3",
        "category_name": "外语",
        "topics": [
            "新概念英语", "大学英语", "英语语法", "英语词汇", "英语写作",
            "英语口语", "英语听力", "商务英语", "翻译理论与实践",
            "日语入门", "标准日本语", "德语基础", "法语教程", "俄语语法",
            "雅思词汇", "托福写作", "GRE 阅读", "考研英语",
            "英语语言学", "跨文化交际", "英文名著导读",
        ],
        "publishers": [
            "外语教学与研究出版社", "上海外语教育出版社",
            "商务印书馆", "世界图书出版公司", "华东理工大学出版社",
            "中国对外翻译出版公司", "大连理工大学出版社",
        ],
        "authors": [
            "L.G. Alexander", "薄冰", "张道真", "许国璋", "胡壮麟",
            "何其莘", "王力", "俞敏洪", "梅德明", "文秋芳",
            "Raymond Murphy", "Michael Swan", "Benny Lewis",
        ],
        "floors": ["五楼南区", "四楼南区", "三楼南区"],
        "class_prefix": "H",
        "tag_pool": ["英语", "语法", "词汇", "听说", "翻译", "考试", "二外", "语言学"],
    },
]

# 位置编号：与中国图书馆分类法的排架习惯一致（分类号/序号）
CLASS_SHELF_NUMBERS = {
    "TP": ["312", "311", "393", "391", "301", "303", "316", "317", "338", "385"],
    "O": ["17", "15", "21", "158", "13", "22", "174", "186", "29", "151"],
    "F": ["0", "23", "27", "275", "830", "713", "224", "406", "121", "272"],
    "B": ["5", "2", "0", "84", "22", "516", "81", "26", "223", "712"],
    "I": ["242", "247", "246", "210", "226", "207", "267", "246", "212", "227"],
    "H": ["31", "319", "314", "315", "315", "365", "335", "334", "059", "0"],
}


# =============================================================================
# ISBN-13 生成与校验
# =============================================================================
def isbn13_check_digit(twelve: str) -> str:
    """按 ISBN-13 规则算校验位：前 12 位交替乘 1、3，和取模 10 后用 10 减。"""
    total = 0
    for index, char in enumerate(twelve):
        digit = int(char)
        total += digit if index % 2 == 0 else digit * 3
    return str((10 - total % 10) % 10)


def build_isbn(sequence: int, rng: random.Random) -> str:
    """
    生成唯一的、校验位合法的 ISBN-13。

    结构：978 + 7(中国组号) + 4 位出版者号 + 4 位书名号 + 1 位校验位

    唯一性证明（这是本函数最重要的一条性质，写死在这里方便复查）：
      令 publisher = 1000 + (sequence % 9000)   -- 取值范围 1000..9999，恰好 4 位
         item      = sequence // 9000           -- 零填充成 4 位
      映射 sequence -> (publisher, item) 是单射，两者又都是定长 4 位，
      因此 sequence1 != sequence2 => 前 12 位字符串必然不同 => ISBN 不同。
      可覆盖 9000 * 10000 = 9000 万个不同序列，远超 §1.1 的 58 万种需求。

    历史踩坑记录（不要"优化"回去）：
      * 早期版本对 item 加 randrange(0,3) 抖动、且 publisher/item 不做零填充，
        结果 3 万条里只有 24507 条唯一 —— 前者让 item 坐标重叠，
        后者让 "1000"+"1" 与 "100"+"01" 拼出同一个字符串。
        两个坑都会直接撞上 book.isbn 的唯一索引 uk_book_isbn。
      * 想要"看起来不规律"应该改标题/作者词表，而不是动 ISBN 的位结构。
    """
    publisher = 1000 + (sequence % 9000)
    item = sequence // 9000
    twelve = "9787{:04d}{:04d}".format(publisher, item)
    return twelve + isbn13_check_digit(twelve)


def is_valid_isbn13(isbn: str) -> bool:
    """自检用：长度 13、全数字、校验位正确。"""
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    return isbn13_check_digit(isbn[:12]) == isbn[12]


# =============================================================================
# 单条记录生成
# =============================================================================
def build_title(profile: dict, rng: random.Random) -> str:
    """书名 = 前缀(可为空) + 主题词 + 后缀。"""
    prefix = rng.choice(TITLE_PREFIXES)
    topic = rng.choice(profile["topics"])
    suffix = rng.choice(TITLE_SUFFIXES)
    title = "{}{}{}".format(prefix, topic, suffix)
    # 书名上限 200 字符（schema.sql: varchar(200)），随机组合一般远小于此，
    # 但保留截断以保证"生成的 SQL 一定不会因为超长而失败"
    return title[:200]


def build_shelf_location(profile: dict, rng: random.Random) -> str:
    """索书号：楼层区 + 分类号 + 序号，形如 '三楼东区 TP312/1287'。"""
    floor = rng.choice(profile["floors"])
    class_prefix = profile["class_prefix"]
    shelf = rng.choice(CLASS_SHELF_NUMBERS[class_prefix])
    number = rng.randrange(1, 2600)
    return "{} {}{}/{}".format(floor, class_prefix, shelf, number)


def build_summary(title: str, author: str, profile: dict, rng: random.Random) -> str:
    """简介：由模板拼装，避免所有书长一样（评审时一眼看出是造的）。"""
    templates = [
        "本书围绕{title}展开，系统讲解核心概念、实现原理与工程实践，配套大量可运行示例，适合{authorshort}方向的读者系统学习。",
        "{title}从基础到进阶逐步铺开，每章配有练习题与解析，是{category}领域被广泛采用的教材之一。",
        "作者结合多年一线经验编写{title}，重点剖析常见误区与性能陷阱，并给出可落地的优化清单。",
        "{title}覆盖{category}的主流方法与前沿进展，兼顾理论推导与动手实验，适合课程教学与自学。",
        "本书是{title}的修订版，新增了近年来业界关注的实践案例，并补充了配套代码与数据集说明。",
    ]
    summary = rng.choice(templates).format(
        title=title,
        author=author,
        authorshort=author.split()[-1] if author.split() else "相关",
        category=profile["category_name"],
    )
    return summary[:1000]


def build_tags(profile: dict, rng: random.Random, keywords) -> str:
    """标签：分类词池随机取 3~5 个 + 书名里的关键词，用于检索加权。"""
    tags = rng.sample(profile["tag_pool"], k=min(len(profile["tag_pool"]), rng.randrange(3, 6)))
    for word in keywords:
        if word and word not in tags:
            tags.append(word)
    return ",".join(tags)[:255]


def extract_keywords(title: str, topic: str) -> list:
    """从书名里抽出可当标签/检索关键词的片段（中文 2-gram + 英文单词）。"""
    words = []
    if topic:
        words.append(topic)
    # 英文术语（Java / MySQL / Spring Boot 之类）
    for token in title.replace("/", " ").replace("-", " ").split():
        cleaned = token.strip("，。、：（）()")
        if cleaned and cleaned.isascii() and cleaned[0].isalpha() and len(cleaned) > 1:
            words.append(cleaned)
    return words[:4]


def build_price(profile: dict, rng: random.Random) -> float:
    """定价：按分类给区间，再用常见尾数收口（.00 / .80 / .00）使其像真实定价。"""
    ranges = {
        "TP": (39.0, 189.0),
        "O1": (25.0, 98.0),
        "F2": (35.0, 158.0),
        "B0": (28.0, 128.0),
        "I2": (22.0, 118.0),
        "H3": (25.0, 99.0),
    }
    low, high = ranges.get(profile["code"], (30.0, 120.0))
    price = rng.uniform(low, high)
    endings = [0.0, 0.8, 0.0, 0.5, 0.0, 0.9]
    price = round(int(price) + rng.choice(endings), 2)
    return price


def generate_records(count: int, seed: int, start_id: int, avg_copies: float):
    """
    生成器：逐条 yield 记录 dict。

    为什么用生成器而不是一次性返回 list：
    58 万条记录的 list 在 2G 内存的机器上会有明显压力，流式消费更稳。
    """
    rng = random.Random(seed)
    copies_rng = random.Random(seed + 1)
    for index in range(count):
        profile = CATEGORY_PROFILES[index % len(CATEGORY_PROFILES)]
        # 让分类分布更接近真实馆藏：计算机类占比略高（图书馆里最常被检索）
        roll = rng.random()
        if roll < 0.34:
            profile = CATEGORY_PROFILES[0]
        elif roll < 0.48:
            profile = CATEGORY_PROFILES[1]
        elif roll < 0.62:
            profile = CATEGORY_PROFILES[4]

        title = build_title(profile, rng)
        author = rng.choice(profile["authors"])
        publisher = rng.choice(profile["publishers"])
        topic = rng.choice(profile["topics"])
        year = rng.choice([2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024])
        total_copies = max(1, int(round(copies_rng.gauss(avg_copies, 1.2))))
        total_copies = min(total_copies, 30)

        isbn = build_isbn(index, rng)
        # 脚本级自检：ISBN 必须合法。造数据造出非法 ISBN 会让人怀疑整份数据的可信度，
        # 所以在生成阶段就用断言暴露（断言信息带 index，便于定位到具体条数）。
        assert is_valid_isbn13(isbn), "生成的 ISBN 校验位不合法：{} (index={})".format(isbn, index)

        yield {
            "id": start_id + index,
            "isbn": isbn,
            "title": title,
            "author": author,
            "publisher": publisher,
            "category_code": profile["code"],
            "publish_year": year,
            "price": build_price(profile, rng),
            "tags": build_tags(profile, rng, extract_keywords(title, topic)),
            "summary": build_summary(title, author, profile, rng),
            "shelf_location": build_shelf_location(profile, rng),
            "total_copies": total_copies,
        }


# =============================================================================
# SQL 输出
# =============================================================================
SQL_HEADER_TEMPLATE = """-- =============================================================================
-- SmartLib 批量书目数据（由 scripts/gen_books.py 自动生成，请勿手工编辑）
--
-- 生成参数：
--   count       = {count}
--   每本平均册数 = {avg_copies}
--   seed        = {seed}
--   start-id    = {start_id}   (book.id 起始值)
--   生成时间     = {generated_at}
--
-- 使用方式（MySQL）：
--   mysql -h 127.0.0.1 -uroot -p smartlib < books_bulk.sql
--   # 或进容器：docker exec -i smartlib-mysql mysql -uroot -p*** smartlib < books_bulk.sql
--
-- 【顺序很重要】
--   1) 先执行本文件（灌数据）
--   2) 再执行 deploy/sql/mysql-production-extras.sql 建 ngram 全文索引
--   反过来会慢很多：FULLTEXT 索引会在每一批 INSERT 时维护倒排表（写放大）。
--
-- 【前提假设】
--   * book 与 book_stock 表已存在（deploy/sql/01-schema.sql）
--   * book_stock 通过 (SELECT id FROM book WHERE isbn = ...) 关联，
--     因此【必须先插入 book，再插入 book_stock】（本文件内部就是这个顺序）
--   * book.id 由自增生成，本文件不显式写 id；--start-id 只用于校对期望值，
--     实际 id 由 MySQL 自增序列决定。要精确控制 id 就手工 ALTER TABLE ... AUTO_INCREMENT。
-- =============================================================================

SET NAMES utf8mb4;
SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';
SET autocommit = 0;
SET unique_checks = 0;
SET foreign_key_checks = 0;

"""

SQL_FOOTER = """
-- 先提交，再自检：autocommit=0，不提交的话下面的 COUNT 看不到刚插入的数据
COMMIT;

SET unique_checks = 1;
SET foreign_key_checks = 1;
SET autocommit = 1;

-- 自检：期望 book 行数与 book_stock 行数都等于生成条数
SELECT COUNT(*) AS book_total FROM book;
SELECT COUNT(*) AS book_stock_total FROM book_stock;
SELECT COUNT(*) AS negative_stock_rows
FROM book_stock WHERE available_copies < 0;
-- 对账自检：期望 0 行（有行说明库存与流水不守恒，对应 GET /api/admin/reconcile）
SELECT s.book_id, s.available_copies, COALESCE(SUM(l.delta), 0) AS ledger_delta_sum
FROM book_stock s
LEFT JOIN stock_ledger l ON l.book_id = s.book_id
GROUP BY s.book_id, s.available_copies, s.total_copies
HAVING s.available_copies <> s.total_copies + COALESCE(SUM(l.delta), 0);
"""


def sql_escape(value: str) -> str:
    """SQL 字符串转义：只需处理单引号和反斜杠（书名里会出现 ' 和 \\）。"""
    return value.replace("\\", "\\\\").replace("'", "''")


def write_sql(records_iter, count: int, out_path: str, batch_size: int, args,
              stats: dict) -> None:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(SQL_HEADER_TEMPLATE.format(
            count=count,
            avg_copies=args.avg_copies,
            seed=args.seed,
            start_id=args.start_id,
            generated_at=generated_at,
        ))
        if args.ignore_duplicates:
            handle.write("-- 已开启 --ignore-duplicates：使用 INSERT IGNORE 跳过 ISBN 冲突的行\n\n")

        insert_keyword = "INSERT IGNORE" if args.ignore_duplicates else "INSERT"
        book_columns = ("(isbn, title, author, publisher, category_id, publish_year, price, tags, "
                        "summary, shelf_location, total_copies, created_at, updated_at)")

        # ---------------------------- 第一遍：book 批次 ----------------------
        handle.write("-- ---------------------------- 书目 ---------------------------\n")
        batch = []
        statements = 0
        for record in records_iter:
            batch.append(record)
            if len(batch) >= batch_size:
                _flush_book_batch(handle, insert_keyword, book_columns, batch)
                stats["books"] += len(batch)
                statements += 1
                batch = []
        if batch:
            _flush_book_batch(handle, insert_keyword, book_columns, batch)
            stats["books"] += len(batch)
            statements += 1
        stats["book_statements"] = statements
        handle.write("\n")

        # ---------------------------- 第二遍：book_stock ---------------------
        # book_stock.book_id 通过 (SELECT id FROM book WHERE isbn = '...') 关联。
        # 为什么不用"自增 id 连续"这个假设：种子数据(data.sql/02-seed.sql)会占用
        # 一段自增区间，写死 id 一旦错位就会插到不存在的书上（或插错书）。
        handle.write("-- ---------------------------- 库存 ---------------------------\n")
        handle.write("-- available_copies = total_copies（初始全部可借），locked_copies = 0\n")
        stock_keyword = "INSERT IGNORE" if args.ignore_duplicates else "INSERT"
        stock_head = ("{} INTO book_stock (book_id, total_copies, available_copies, locked_copies, "
                      "version, updated_at) VALUES\n").format(stock_keyword)
        # header_open：当前是否已写出 INSERT 头但还没有任何行。
        # 这样批次结束时不会留下"只有头没有值"的残缺语句（那是语法错误）。
        header_open = False
        rows_in_statement = 0
        for record in _reiterate(records_iter, args):
            if not header_open:
                handle.write(stock_head)
                header_open = True
                rows_in_statement = 0
            else:
                handle.write(",\n")
            handle.write("  ((SELECT id FROM book WHERE isbn = '{}'), {}, {}, 0, 0, NOW())".format(
                sql_escape(record["isbn"]),
                record["total_copies"],
                record["total_copies"],
            ))
            rows_in_statement += 1
            stats["stocks"] += 1
            if rows_in_statement >= batch_size:
                handle.write(";\n\n")
                stats["stock_statements"] += 1
                header_open = False
        if header_open:
            handle.write(";\n")
            stats["stock_statements"] += 1
        handle.write(SQL_FOOTER)


def _flush_book_batch(handle, insert_keyword: str, book_columns: str, batch: list) -> None:
    handle.write("{} INTO book {} VALUES\n".format(insert_keyword, book_columns))
    rows = []
    for record in batch:
        rows.append(
            "  ('{}', '{}', '{}', '{}', (SELECT id FROM category WHERE code='{}'), {}, {:.2f}, "
            "'{}', '{}', '{}', {}, NOW(), NOW())".format(
                sql_escape(record["isbn"]),
                sql_escape(record["title"]),
                sql_escape(record["author"]),
                sql_escape(record["publisher"]),
                sql_escape(record["category_code"]),
                record["publish_year"],
                record["price"],
                sql_escape(record["tags"]),
                sql_escape(record["summary"]),
                sql_escape(record["shelf_location"]),
                record["total_copies"],
            )
        )
    handle.write(",\n".join(rows))
    handle.write(";\n")


def _reiterate(records_iter, args):
    """
    SQL 模式需要两遍数据（book 一遍、book_stock 一遍）。
    生成器已被消费，因此第二遍用同样的 seed 重新生成——确定性保证了两次一致。
    这样内存占用仍是常量级，不用把 58 万条记录留在内存里。
    """
    return generate_records(args.count, args.seed, args.start_id, args.avg_copies)


# =============================================================================
# CSV 输出
# =============================================================================
BOOK_CSV_COLUMNS = ["isbn", "title", "author", "publisher", "category_code",
                    "publish_year", "price", "tags", "summary", "shelf_location", "total_copies"]
STOCK_CSV_COLUMNS = ["isbn", "total_copies", "available_copies", "locked_copies", "version"]


def write_csv(records_iter, out_dir: str, stats: dict) -> None:
    book_path = os.path.join(out_dir, "books_bulk.csv")
    stock_path = os.path.join(out_dir, "book_stock_bulk.csv")
    with open(book_path, "w", encoding="utf-8", newline="") as book_file, \
            open(stock_path, "w", encoding="utf-8", newline="") as stock_file:
        book_writer = csv.DictWriter(book_file, fieldnames=BOOK_CSV_COLUMNS,
                                     extrasaction="ignore", lineterminator="\n")
        stock_writer = csv.DictWriter(stock_file, fieldnames=STOCK_CSV_COLUMNS,
                                      lineterminator="\n")
        book_writer.writeheader()
        stock_writer.writeheader()
        for record in records_iter:
            book_writer.writerow(record)
            stats["books"] += 1
            stock_writer.writerow({
                "isbn": record["isbn"],
                "total_copies": record["total_copies"],
                "available_copies": record["total_copies"],
                "locked_copies": 0,
                "version": 0,
            })
            stats["stocks"] += 1


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="gen_books.py",
        description="SmartLib 书目/库存测试数据生成器（纯标准库，可复现）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/gen_books.py --count 200 --out deploy/sql/generated/\n"
            "  python scripts/gen_books.py --count 200000 --avg-copies 2\n"
            "  python scripts/gen_books.py --count 5000 --format csv --seed 7\n"
        ),
    )
    parser.add_argument("--count", type=int, default=200000,
                        help="生成书目条数（默认 200000；冒烟测试用 200）")
    parser.add_argument("--out", default=None,
                        help="输出目录（默认 deploy/sql/generated/，相对仓库根目录）")
    parser.add_argument("--format", choices=["sql", "csv"], default="sql",
                        help="输出格式（默认 sql）")
    parser.add_argument("--seed", type=int, default=20251008,
                        help="随机种子：同一种子生成结果完全一致（默认 20251008）")
    parser.add_argument("--start-id", type=int, default=1,
                        help="期望的 book.id 起始值，仅用于文件头注释与校对（默认 1）。"
                             "注意生成的 SQL 不显式写 id，实际 id 由 MySQL 自增序列决定；"
                             "要让首个生成书目拿到某个 id，请先执行 "
                             "ALTER TABLE book AUTO_INCREMENT = <值>。")
    parser.add_argument("--avg-copies", type=float, default=1.6,
                        help="每本书平均复本数（默认 1.6；要造 120 万册就配 --count 580000 --avg-copies 2）")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="每条 INSERT 语句包含的行数（默认 500，太大可能超 max_allowed_packet）")
    parser.add_argument("--ignore-duplicates", action="store_true",
                        help="用 INSERT IGNORE 跳过 ISBN 冲突（配合已有种子数据时用）")
    parser.add_argument("--quiet", action="store_true", help="只输出最终统计，不打印进度")
    return parser.parse_args(argv)


def resolve_out_dir(raw_out, fmt: str):
    """
    输出目录解析规则（返回 (目录, 显式文件名或 None)）：
      * 未指定：<仓库根>/deploy/sql/generated/
      * 以路径分隔符结尾 / 已存在目录 / 没有扩展名：当成目录
      * --format sql 且以 .sql 结尾：当成完整文件路径（其余情况按目录处理）
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if raw_out is None:
        return os.path.join(repo_root, "deploy", "sql", "generated"), None

    candidate = raw_out
    if not os.path.isabs(candidate):
        candidate = os.path.join(repo_root, candidate)

    if fmt == "sql" and candidate.lower().endswith(".sql"):
        return os.path.dirname(candidate) or ".", candidate

    # 有扩展名但不是 .sql（如 foo.csv）说明用户误用，直接报错比静默建目录好
    tail = os.path.basename(candidate)
    if "." in tail and not os.path.isdir(candidate):
        raise ValueError(
            "--out 指向了带扩展名的文件 '{}'，但 --format={} 输出的是固定文件名。"
            "请传目录（如 deploy/sql/generated/），或 --format sql 时传 .sql 文件路径。".format(
                raw_out, fmt))

    return candidate.rstrip("/\\"), None


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.count <= 0:
        print("[gen_books] 错误：--count 必须为正整数", file=sys.stderr)
        return 2
    if args.batch_size <= 0:
        print("[gen_books] 错误：--batch-size 必须为正整数", file=sys.stderr)
        return 2
    if args.avg_copies <= 0:
        print("[gen_books] 错误：--avg-copies 必须为正数", file=sys.stderr)
        return 2

    try:
        out_dir, explicit_file = resolve_out_dir(args.out, args.format)
    except ValueError as error:
        print("[gen_books] 错误：{}".format(error), file=sys.stderr)
        return 2

    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    stats = {"books": 0, "stocks": 0, "stock_statements": 0, "book_statements": 0}
    started = datetime.now()

    if not args.quiet:
        print("[gen_books] 输出目录 : {}".format(os.path.abspath(out_dir)))
        print("[gen_books] 生成条数 : {}".format(args.count))
        print("[gen_books] 格式     : {}".format(args.format))
        print("[gen_books] 随机种子 : {}".format(args.seed))

    if args.format == "sql":
        out_path = explicit_file or os.path.join(out_dir, "books_bulk.sql")
        records = generate_records(args.count, args.seed, args.start_id, args.avg_copies)
        write_sql(records, args.count, out_path, args.batch_size, args, stats)
        size_mb = os.path.getsize(out_path) / 1024.0 / 1024.0
        print("[gen_books] 已写出 : {} ({:.2f} MB)".format(out_path, size_mb))
        print("[gen_books] book 行数 {} / book_stock 行数 {} / 语句数 {}"
              .format(stats["books"], stats["stocks"], stats["book_statements"]))
    else:
        records = generate_records(args.count, args.seed, args.start_id, args.avg_copies)
        write_csv(records, out_dir, stats)
        book_path = os.path.join(out_dir, "books_bulk.csv")
        stock_path = os.path.join(out_dir, "book_stock_bulk.csv")
        print("[gen_books] 已写出 : {} ({:.2f} MB)".format(
            book_path, os.path.getsize(book_path) / 1024.0 / 1024.0))
        print("[gen_books] 已写出 : {} ({:.2f} MB)".format(
            stock_path, os.path.getsize(stock_path) / 1024.0 / 1024.0))
        print("[gen_books] book 行数 {} / book_stock 行数 {}".format(stats["books"], stats["stocks"]))

    elapsed = (datetime.now() - started).total_seconds()
    print("[gen_books] 耗时 {:.2f}s（{:.0f} 条/秒）".format(
        elapsed, stats["books"] / elapsed if elapsed > 0 else 0))
    print("[gen_books] 提示：先灌本文件，再执行 deploy/sql/mysql-production-extras.sql 建 ngram 全文索引")
    return 0


if __name__ == "__main__":
    sys.exit(main())
