from opentrons import protocol_api

metadata = {'protocolName': 'AI Generated Protocol', 'author': 'Biolab AI', 'apiLevel': '2.15'}

def run(protocol: protocol_api.ProtocolContext):
    lw_3 = protocol.load_labware('corning_96_wellplate_360ul_flat', '3', label='dest_plate')
    lw_2 = protocol.load_labware('corning_96_wellplate_360ul_flat', '2', label='source_plate')
    lw_1 = protocol.load_labware('opentrons_96_tiprack_300ul', '1', label='tiprack')

    pip_left = protocol.load_instrument('p300_single_gen2', 'left', tip_racks=[lw_1])

    pip_left.transfer(50.0, lw_2['A1'], lw_3['B1'])
    pip_left.transfer(50.0, lw_2['A2'], lw_3['B2'])
    pip_left.transfer(50.0, lw_2['A3'], lw_3['B3'])