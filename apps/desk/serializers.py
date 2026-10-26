from rest_framework import serializers
from .models import ConversationMessage, Ticket

class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ["id", "title", "description", "status", "customer_id", "assigned_agent_id", "version", "created_at", "updated_at"]
        read_only_fields = ["id", "status", "customer_id", "assigned_agent_id", "version", "created_at", "updated_at"]

class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConversationMessage
        fields = ["id", "ticket_id", "sender_id", "client_message_id", "body", "created_at", "read_at"]
        read_only_fields = ["id", "ticket_id", "sender_id", "created_at", "read_at"]

class AssistantRequestSerializer(serializers.Serializer):
    ticket_id = serializers.IntegerField()
    question = serializers.CharField(max_length=2000)


class StatusTransitionSerializer(serializers.Serializer):
    """An optimistic-concurrency transition request.

    Tickets deliberately have a small state machine: an assigned agent (or an
    administrator) can resolve a claimed ticket.  Claiming remains its own
    operation because it assigns the participant as well as changing state.
    """

    status = serializers.ChoiceField(choices=[Ticket.Status.RESOLVED])
    expected_version = serializers.IntegerField(min_value=1)
