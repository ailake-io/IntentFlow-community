if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader


@data_loader
def load(*args, **kwargs):
    return [1, 2, 3]
