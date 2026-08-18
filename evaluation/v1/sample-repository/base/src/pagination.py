def page_items(items, page_size):
    return items[0:page_size]


def total_pages(count, page_size):
    return (count + page_size - 1) // page_size
