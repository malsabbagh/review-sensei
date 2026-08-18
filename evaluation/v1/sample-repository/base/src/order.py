def total(order):
    return sum(item.price for item in order.items)
