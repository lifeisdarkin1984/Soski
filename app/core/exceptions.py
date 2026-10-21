class CollectorBaseException(Exception):
    pass

class AccountAuthError(CollectorBaseException):
    pass

class BotInteractionError(CollectorBaseException):
    pass

class ChannelJoinError(CollectorBaseException):
    pass
