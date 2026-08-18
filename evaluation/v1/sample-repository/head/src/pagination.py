def page_items(items, page_size, page=1):
    start = (page - 1) * page_size
    return items[start : start + page_size]


def total_pages(count, page_size):
    return (count + page_size - 1) // page_size
