from ..utils.config import settings
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from typing import Union

# The Collection Public Key provided by Dialectica

def encrypt_vote(vote_val: Union[str, int]) -> bytes:
    """
    Encrypts the vote (string or integer) using RSA-OAEP.
    If integer provided (1 or 2), it is converted to string for encryption.
    """
    # Load the PEM public key
    public_key = load_pem_public_key(settings.DIALECTICA_PUBLIC_KEY.encode('utf-8'))
    
    # Handle int -> str conversion for backend compatibility
    # Ensure payload is bytes before encryption
    payload = str(vote_val).encode('utf-8') if isinstance(vote_val, int) else vote_val.encode('utf-8')

    # Encrypt using RSA-OAEP 
    encrypted_vote = public_key.encrypt(
        payload,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    
    return encrypted_vote # Guarantees a 256-byte payload
