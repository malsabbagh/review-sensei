def total(order):
    return sum(item.price for item in order.items)


def apply_discount(order, rate):
    return total(order) * (1 - rate)
