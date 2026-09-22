"""Contract + topic registry, checked against Bitquery primary docs on 2026-09-22.

Source: https://docs.bitquery.io/docs/blockchain/arc-mainnet/arc-mainnet-launchpads-api/
Minara: https://docs.bitquery.io/docs/blockchain/arc-mainnet/minara-launchpad-api/
Historical deployments beyond these addresses are not asserted to be covered.
"""
REGISTRY = {
    '0xb021be536808f551b31789422fd28a6c9c6e97da': dict(name='Argus', token_topic=1, creator_topic=2,
        signature='1d8917231579f8ce39407f0d616f36f357b07329b0ce5164d0754ac15145ce0a'),
    '0x4b638c1502a07a8e1a26112ee98f51a3f34bc93a': dict(name='RadarDEX Classic', token_topic=1,
        signature='851d681a32f0efba577c4a1bd412f74b575764a6b91e499a05a48a23f3821d66'),
    '0x2d933ce4bde6f3d99540b5d7886b383e59b2b2f8': dict(name='RadarDEX Reflection', token_topic=1,
        signature='851d681a32f0efba577c4a1bd412f74b575764a6b91e499a05a48a23f3821d66'),
    '0xcad7ee36ac193bf2eddb7b3e2736c5bdb8269c8b': dict(name='Tolly', token_topic=1,
        signature='875522b092d9e19a1de359e4bd218090d582fa521c9733889acf1a5ff1941255'),
    '0x0dcad158e98bc24455f9e94f46709d8a5f6d1255': dict(name='Warp', token_topic=1,
        signature='0b4cfda446fdf9ec5a85855f088c154869eb62e3e723d7d80319b680f90e0cfd'),
    '0x297cebc4de347347205cd08667b56ee951dd8810': dict(name='Archemist V2', token_topic=1,
        signature='8e83c293b82cf6e864a90c1ccffea5e0f1ec23b271eff78e78f1dbd5e32a9c7d'),
    '0xd0aa679ec263e8f9bc929426eb9eab2e061d2c5f': dict(name='PEGD V4', token_topic=2,
        signature='4b5a1abdb5ebec3e01fa29e6e1f5e3095f8b3cfaed9ffcd46ad35e7ebc58039e'),
    '0x4d3a3f4e1a918845c2038bc064c4d250822b203e': dict(name='Minara', token_topic=2, pool_topic=1,
        signature='3b3d2bafdcae274a232217e1f80ee4305d3af6aa25c8b14b1681bd68d18042a4'),
}
